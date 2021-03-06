from __future__ import division
import numpy as np
import pandas as pd
from scipy.special import digamma, gammaln, betaln
from scipy.optimize import minimize
from forward_backward import fb_infer
import numexpr as ne
from pdb import set_trace

class GPModel:
    """
    This class represents and fits a Gamma-Poisson model via variational
    inference. Variables are as follows:
    data: data frame with columns 'unit', 'movie', 'frame', and 'count'
    dt: time difference between observations
    K: number of latent categories to fit

    N: M x U array of count observations
    lam: K x U array of _multiplicative_ effects; lam[0] is baseline
    z: T x K array of latent states in the Markov chain (0 or 1)
    A: 2 x 2 x K array of column-stochastic Markov transition matrices (one
        per chain)
    pi: 2 x K array of initial chain state probabilities
    theta: M x U array of trial-to-trial overdispersion parameters

    Model is defined as:
    N_{mu}|z ~ Poisson(theta_{mu} * prod_k mu_{ku}^z_{t(m)k})
    lam_{ku}|z ~ Gamma(alpha_{ku}, beta_{ku})
    A(k)_{i -> 1} ~ Beta(gamma1_{ik}, gamma2_{ik})
    pi(k)_1 ~ Beta(delta1_k, delta2_k)

    Priors are:
    lam_{ku}|z ~ Gamma(cc_{ku}, dd_{ku})
    A(k)_{i -> 1} ~ Beta(nu1_{ik}, nu2_{ik})
    pi(k)_1 ~ Beta(rho1_k, rho2_k)
    theta_{mu} ~ Gamma(ss_u, rr_u)

    Derived parameters:
    xi_{tk} = E[z_{tk}]
    Xi_{t,k}[j, i] = p(z_{t+1, k}=j, z_{tk}=i)
    """
    def __init__(self, data, K, dt, include_baseline=False, overdispersion=False, regression_updater='approximate'):
        """
        Set up basic constants for the model. 
        """
        M = data.shape[0]
        # J = number of regressors; excludes movie, frame, unit, count
        J = data.shape[1] - 4  
        T = data[['movie', 'frame']].drop_duplicates().shape[0]
        U = data['unit'].drop_duplicates().shape[0]
        self.M = M
        self.J = J
        self.T = T
        self.K = K
        self.U = U
        self.dt = dt
        self.overdispersion = overdispersion
        self.regressors = self.J > 0

        self.updater = regression_updater

        self.include_baseline = include_baseline
        self.prior_pars = ({'lam_prior_mean': (2, K), 'lam_prior_var': (2, K),
            'nu1': (2, K), 'nu2': (2, K), 'rho1': (K,), 'rho2': (K,)})
        self.variational_pars = ({'alpha': (K, U), 'beta': (K, U), 
            'lam_post_mean': (2, K), 'lam_post_var': (2, K),
            'gamma1': (2, K), 'gamma2': (2, K), 
            'delta1': (K,), 'delta2': (K,), 'xi': (T, K), 
            'Xi': (T - 1, K, 2, 2), 'logq': (K,)})

        if self.overdispersion:
            self.prior_pars.update({'ss': (U,), 'rr': (U,)})
            self.variational_pars.update({'omega': (M,), 'zeta': (M,)})

        if self.regressors:
            self.prior_pars.update({'vv': (J, U), 'ww': (J, U)})
            self.variational_pars.update({'aa': (J, U), 'bb': (J, U)})


        self.log = {'L':[], 'H':[]}  # for debugging
        self.Lvalues = []  # for recording value of optimization objective

        self._set_data(data)

    def set_priors(self, **kwargs):
        """
        Set prior parameters for inference. See definitions above.
        """
        for var, shape in self.prior_pars.iteritems():
            if var not in kwargs:
                setattr(self, var, np.ones(shape))
            elif kwargs[var].shape == shape:
                setattr(self, var, kwargs[var].copy())
            else:
                raise ValueError(
                    'Prior on parameter {} has incorrect shape'.format(var))

        return self

    def set_inits(self, **kwargs):
        """
        Set variational parameters for inference. See definitions above.
        """
        for var, shape in self.variational_pars.iteritems():
            if var not in kwargs:
                setattr(self, var, np.ones(shape))
            elif kwargs[var].shape == shape:
                setattr(self, var, kwargs[var].copy())
            else:
                raise ValueError(
                    'Prior on variational parameter {} has incorrect shape'.format(var))

        self.logZ = np.zeros(self.K)  # normalization constant for each chain

        # make sure baseline category turned on
        if self.include_baseline:
            self.xi[:, 0] = 1

            # ensure p(z_{t+1}=1, z_t=1) = 1
            self.Xi[:, 0] = 0
            self.Xi[:, 0, 1, 1] = 1

        # normalize Xi
        self.Xi = self.Xi / np.sum(self.Xi, axis=(-1, -2), keepdims=True)

        # initialize F product
        self.F_prod(update=True)

        # initialize G product
        self.G_prod(update=True)

        # intialize H
        self.H = 0

        return self

    def _set_data(self, Nframe):
        """
        Nframe is a dataframe containing columns unit, movie, frame, and 
        count. Any additional columns will be considered external regressors.
        """

        Nf = Nframe[['unit', 'movie', 'frame', 'count']]

        # make an array of all spikes for a given time within movie and 
        # unit; this only has to be done once
        countframe = Nf.groupby(['movie', 'frame', 'unit']).sum().unstack(level=2)
        countarr = countframe.values
        self.N = np.ma.masked_where(np.isnan(countarr), countarr).astype('int')

        # make a dataframe linking movie and frame to a unique time index
        self.t_index = Nf[['movie', 'frame']].drop_duplicates().reset_index(drop=True)
        self.t_index.index.name = 'time'
        self.t_index = self.t_index.reset_index()

        # make a frame of presentations linked to time index
        allframe = pd.merge(self.t_index, Nframe)
        self.Nframe = allframe[['unit', 'time', 'count']].copy()

        # make a frame of regressors indexed the same way
        self.Xframe = allframe.drop(['frame', 'movie', 'unit', 'count', 'time'], axis=1).copy()

        # set unit to start at 0
        self.Nframe['unit'] = self.Nframe['unit'] - np.min(self.Nframe['unit'])

        # make a masked array counting the number of observations of each 
        # (time, unit) pair
        Nobs = self.Nframe.groupby(['time', 'unit']).count().unstack()
        self.Nobs = np.ma.masked_where(np.isnan(Nobs), Nobs).astype('int')

        return self

    def F_prod(self, k=None, update=False):
        """
        Accessor method to return the value of the F product.
        If k is specified, return F_{tku} (product over all but k), 
        else return F_{tu} (product over all k). If update=True, 
        recalculate F before returning the result and cache the new 
        matrix.
        """
        if update:
            if k is not None:
                zz = self.xi[:, k, np.newaxis]
                w = self.alpha[k] / self.beta[k]
                vv = ne.evaluate("1 - zz + zz * w")
                self._Fpre[:, k, :] = vv
            else:
                zz = self.xi[:, :, np.newaxis]
                w = (self.alpha / self.beta)
                vv = ne.evaluate("1 - zz + zz * w")
                self._Fpre = vv

            # work in log space to avoid over/underflow
            Fpre = self._Fpre
            dd = ne.evaluate("sum(log(Fpre), axis=1)")
            self._Ftu = ne.evaluate("exp(dd)")
            ddd = dd[:, np.newaxis, :]
            self._Ftku = ne.evaluate("exp(ddd - log(Fpre))")

        if k is not None:
            return self._Ftku[:, k, :]
        else:
            return self._Ftu

    def G_prod(self, k=None, update=False):
        """
        Return the value of the G product.
        If k is specified, return G_{tku} (product over all but k),
        else return G_{tu} (product over all k). If update=True,
        recalculate G before returning the result and cache the new 
        matrix.
        
        NOTE: Because the regressors may vary by *presentation* and not 
        simply by movie time, the regressors are in a "melted" dataframe
        with each (unit, presentation) pair in a row by itself. As a 
        result, X is (M, J), G_{tu} is (M,), and G_{tku} is (M, J).
        """
        if update:
            uu = self.Nframe['unit']
            tt = self.Nframe['time']
            if k is not None:
                zz = (self.aa[k] / self.bb[k])[uu]
                # get x values for kth regressor
                xx = self.Xframe.values[:, k]
                vv = ne.evaluate("zz ** xx")
                self._Gpre[:, k] = vv
            else:
                zz = (self.aa / self.bb)[:, uu].T
                # get x values for kth regressor; col 0 = time
                xx = self.Xframe.values
                vv = ne.evaluate("zz ** xx")
                self._Gpre = vv

            # work in log space to avoid over/underflow
            Gpre = self._Gpre
            dd = ne.evaluate("sum(log(Gpre), axis=1)")
            self._Gtu = ne.evaluate("exp(dd)")
            ddd = dd[:, np.newaxis]
            self._Gtku = ne.evaluate("exp(ddd - log(Gpre))")

        if k is not None:
            return self._Gtku[:, k]
        else:
            return self._Gtu

    def calc_log_A(self):
        """
        Calculate E[log A] with A the Markov chain transition matrix.
        Return exp(E[log A]), which is the parameter value to be used 
        in calculating updates to the latent states. Note that this 
        A is NOT column stochastic, since its E[log A] is set via 
        the variational update rules (cf Beal, 2003).
        """
        # as per Beal's thesis, these are subadditive, but this is 
        # compensated for by normalization in fb algorithm
        # necessary to correctly calculate logZ
        log_A = np.empty((2, 2, self.K))
        log_A[1]  = digamma(self.gamma1) - digamma(self.gamma1 + self.gamma2)
        log_A[0]  = digamma(self.gamma2) - digamma(self.gamma1 + self.gamma2)
        return log_A

    def calc_log_pi(self):
        """
        Calculate E[log pi] with pi the Markov chain initial state 
        probability. Return exp of this, which is the parameter value to
        be used in calculating updates to the latent states. Note that this 
        does NOT return a probability (does not sum to one). Cf. Beal 2003.
        """ 
        # as per Beal's thesis, these are subadditive, but this is 
        # compensated for by normalization in fb algorithm
        # necessary to correctly calculate logZ
        log_pi = np.empty((2, self.K))
        log_pi[1] = digamma(self.delta1) - digamma(self.delta1 + self.delta2)
        log_pi[0] = digamma(self.delta2) - digamma(self.delta1 + self.delta2)
        return log_pi

    def calc_log_evidence(self, k):
        """
        Calculate emission probabilities for use in forward-backward 
        algorithm. These are not properly normalized, but that will
        be compensated for in the logZ value returned by fb_infer.
        """
        logpsi = np.empty((self.T, 2))
        bar_log_lambda = digamma(self.alpha) - np.log(self.beta)
        bar_lambda = self.alpha / self.beta
        if self.overdispersion:
            bar_theta = self.omega / self.zeta
        else:
            bar_theta = 1
        Fk = self.F_prod(k)

        # need to account for multiple observations of same frame by same
        # unit, so use Nframe
        N = self.Nframe
        nn = N['count']
        tt = N['time']
        uu = N['unit'] 
        G = self.G_prod()
        Gbar = np.mean(G)  # use as normalizer

        N['lam0'] = -Fk[tt, uu] * bar_theta * G / Gbar
        N['lam1'] = (nn * bar_log_lambda[k, uu] - Fk[tt, uu] * bar_lambda[k, uu] * bar_theta * G) / Gbar

        logpsi = N.groupby('time').sum()[['lam0', 'lam1']].values

        return logpsi

    @staticmethod
    def H_gamma(alpha, beta):
        """
        Calculate entropy of gamma distribution given array parameters
        alpha and beta.
        """
        H = (alpha - np.log(beta) + gammaln(alpha) + (1 - alpha) * digamma(alpha))
        return H

    @staticmethod
    def H_beta(alpha, beta):
        """
        Calculate entropy of beta distribution with parameters alpha and beta.
        """
        H = (betaln(alpha, beta) - (alpha - 1) * digamma(alpha) - 
            (beta - 1) * digamma(beta) + 
            (alpha + beta - 2) * digamma(alpha + beta)) 
        return H

    def L(self, keeplog=False):
        """
        Calculate E[log p - log q] in the variational approximation.
        Here, we calculate by appending to a list and then returning the 
        sum. This is slower, but better for debugging.
        This result is only valid immediately after the E-step (i.e, updating
        E[z] in forward-backward). For a discussion of cancellations that 
        occur in this context, cf. Beal (2003) ~ (3.79).
        """
        ############### useful expectations ################ 
        # lambda mean hyperparameter
        bar_lam_mean = self.lam_post_mean[0] / self.lam_post_mean[1]
        bar_log_lam_mean = digamma(self.lam_post_mean[0]) - np.log(self.lam_post_mean[1])

        # lambda variance hyperparameter        
        bar_lam_var = self.lam_post_var[0] / self.lam_post_var[1]
        bar_log_lam_var = digamma(self.lam_post_var[0]) - np.log(self.lam_post_var[1])

        # lambda
        bar_log_lambda = digamma(self.alpha) - np.log(self.beta)
        bar_lambda = self.alpha / self.beta
        bar_shape = bar_lam_var
        bar_rate = bar_lam_mean * bar_lam_var

        # overdispersion
        if self.overdispersion:
            bar_theta = self.omega / self.zeta
            bar_log_theta = digamma(self.omega) - np.log(self.zeta)
        else:
            bar_theta = 1
            bar_log_theta = 0

        # regression coefficients
        if self.regressors:
            bar_upsilon = self.aa / self.bb
            bar_log_upsilon = digamma(self.aa) - np.log(self.bb)
        else:
            bar_upsilon = 1
            bar_log_upsilon = 0

        uu = self.Nframe['unit']
        tt = self.Nframe['time']
        nn = self.Nframe['count']
        if self.regressors:
            xx = self.Xframe.values

        L = [] 
        ############### E[log (p(pi) / q(pi))] #############
        logpi = self.calc_log_pi()
        L.append(np.sum((self.rho1 - 1) * logpi[1] + (self.rho2 - 1) * logpi[0]- betaln(self.rho1, self.rho2)))
        H_pi = self.H_beta(self.delta1, self.delta2)
        L.append(np.sum(H_pi))

        ############### E[log (p(A) / q(A))] #############
        logA = self.calc_log_A()
        L.append(np.sum((self.nu1 - 1) * logA[1] + (self.nu2 - 1) * logA[0] - betaln(self.nu1, self.nu2)))
        H_A = self.H_beta(self.gamma1, self.gamma2)
        L.append(np.sum(H_A))

        ############### E[log (p(lambda|shape, rate) / q(lambda))] #######
        L.append(np.sum((bar_shape[:, np.newaxis] - 1) * bar_log_lambda))
        L.append(-np.sum(bar_rate[:, np.newaxis] * bar_lambda))

        # these pieces approximate alpha log beta - log Gamma(alpha)
        L.append(self.U * np.sum(bar_lam_var - 1))
        L.append(self.U * np.sum(bar_lam_var * bar_log_lam_mean))
        L.append(0.5 * self.U * np.sum(bar_log_lam_var))

        H_lambda = self.H_gamma(self.alpha, self.beta)
        L.append(np.sum(H_lambda))

        ############### E[log (p(lam_mean) / q(lam_mean))] #############
        L.append(np.sum((self.lam_prior_mean[0] - 1) * bar_log_lam_mean))
        L.append(-np.sum(self.lam_prior_mean[1] * bar_lam_mean))

        H_lam_mean = self.H_gamma(self.lam_post_mean[0], self.lam_post_mean[1])
        L.append(np.sum(H_lam_mean))

        ############### E[log (p(lam_var) / q(lam_var))] #############
        L.append(np.sum((self.lam_prior_var[0] - 1) * bar_log_lam_var))
        L.append(-np.sum(self.lam_prior_var[1] * bar_lam_var))

        H_lam_var = self.H_gamma(self.lam_post_var[0], self.lam_post_var[1])
        L.append(np.sum(H_lam_var))

        ############### E[log (p(theta) / q(theta))] #############
        if self.overdispersion:
            L.append((self.ss[uu] - 1).dot(bar_log_theta))
            L.append(-self.rr[uu].dot(bar_theta))
            H_theta = self.H_gamma(self.omega, self.zeta)
            L.append(np.sum(H_theta))

        ############### E[log (p(upsilon) / q(upsilon))] #############
        if self.regressors:
            L.append(np.sum((self.vv - 1) * bar_log_upsilon))
            L.append(-np.sum(self.ww * bar_upsilon))
            H_upsilon = self.H_gamma(self.aa, self.bb)
            L.append(np.sum(H_upsilon))

        ############### E[log (p(N, z|A, pi, lambda) / q(z))] #############
        Npiece = np.sum(self.N[:, np.newaxis, :] * self.xi[..., np.newaxis] * bar_log_lambda[np.newaxis, ...]) 
        if self.overdispersion:
            Npiece += nn.dot(bar_log_theta)
        if self.regressors:
            Npiece += np.sum(nn[:, np.newaxis] * xx * bar_log_upsilon[:, uu].T)
        L.append(Npiece)
        L.append(-np.sum(self.F_prod()[tt, uu] * bar_theta * self.G_prod()))

        logpi = self.calc_log_pi()
        L.append(np.sum((1 - self.xi[0]) * logpi[0] + self.xi[0] * logpi[1]))

        logA = self.calc_log_A()
        L.append(np.sum(self.Xi * logA.transpose((2, 0, 1))))

        # subtract the entropy of q(z) HMM
        L.append(-np.sum(self.logq))
        L.append(np.sum(self.logZ))

        if keeplog:
            self.log['L'].append(L)
            self.log['H'].append(self.H)


        return np.sum(L)

    def update_lambda_mean_hypers(self):
        """
        Update the lambda mean prior parameter.
        """
        bar_lam_var = self.lam_post_var[0] / self.lam_post_var[1]
        bar_lambda = self.alpha / self.beta

        self.lam_post_mean[0] = self.lam_prior_mean[0] + self.U * bar_lam_var
        self.lam_post_mean[1] = self.lam_prior_mean[1] + bar_lam_var * np.sum(bar_lambda, axis=1)

        return self

    def update_lambda_var_hypers(self):
        """
        Update the lambda variance prior parameter. 
        """
        bar_lam_mean = self.lam_post_mean[0] / self.lam_post_mean[1]
        bar_log_lam_mean = digamma(self.lam_post_mean[0]) - np.log(self.lam_post_mean[1])
        bar_lambda = self.alpha / self.beta
        bar_log_lambda = digamma(self.alpha) - np.log(self.beta)

        self.lam_post_var[0] = self.lam_prior_var[0] + 0.5 * self.U 
        self.lam_post_var[1] = self.lam_prior_var[1] - self.U * (1 + bar_log_lam_mean) + np.sum(bar_lam_mean[:, np.newaxis] * bar_lambda - bar_log_lambda, axis=1)

        return self

    def update_lambda(self, k):
        """
        Update parameters corresponding to emission parameters for 
        each Markov chain.
        """
        uu = self.Nframe['unit']
        tt = self.Nframe['time']
        bar_lam_mean = self.lam_post_mean[0] / self.lam_post_mean[1]
        bar_lam_var = self.lam_post_var[0] / self.lam_post_var[1]
        bar_shape = bar_lam_var
        bar_rate = bar_lam_mean * bar_lam_var

        if self.overdispersion:
            bar_theta = self.omega / self.zeta
        else:
            bar_theta = 1
        Nz = self.xi[:, k].dot(self.N)
        Fz = self.F_prod(k) * self.xi[:, k, np.newaxis]

        # G is returned as one row per observation
        # want it to be time x unit
        G_tu = self.G_prod()

        FthG = pd.DataFrame(Fz[tt, uu] * bar_theta * G_tu).groupby(uu).sum().values.squeeze()

        self.alpha[k] = Nz + bar_shape[k]
        self.beta[k] = FthG + bar_rate[k]

        self.F_prod(k, update=True)

        return self

    def update_upsilon(self):
        """
        Update regression coefficient for a particular regressor i.
        """
        nn = self.Nframe['count']
        uu = self.Nframe['unit']
        NX = nn[:, np.newaxis] * self.Xframe

        self.aa = NX.groupby(uu).sum().values.T + self.vv

        self.bb_old = self.bb.copy()
        starts = self.bb
        self.bb = self._get_b(starts)
        self.G_prod(update=True)

        return self

    def _get_b(self, starts):
        """
        Solve for log(b) via black-box optimization. Use log(b) since this
        is the natural parameter.
        Updater is the name of a factory function that returns a function
        to be minimized based on current parameter values.
        """
        if self.updater == 'exact':
            minfun = self._make_exact_minfun()
        elif self.updater == 'approximate':
            minfun = self._make_approximate_minfun()

        eps_starts = np.log(self.aa / starts)
        res = minimize(minfun, eps_starts, jac=True)
        if not res.success:
            print "Warning: optimization terminated without success."
            print res.message
        eps = res.x.reshape(self.J, self.U)
        bb = self.aa * np.exp(-eps)
        return bb

    def _make_exact_minfun(self):
        """
        Factory function that returns a function to be minimized.
        This version uses an exact minimization objective.
        """
        uu = self.Nframe['unit']
        tt = self.Nframe['time']
        if self.overdispersion:
            bar_theta = self.omega / self.zeta
        else:
            bar_theta = 1
        F_prod = self.F_prod()[tt, uu]

        def minfun(epsilon): 
            """
            This is the portion of the evidence lower bound that depends on 
            the b parameter. eps = log(a/b)
            """
            eps = epsilon.reshape(self.J, self.U)
            sum_log_G = np.sum(eps[:, uu].T * self.Xframe.values, axis=1)
            G_prod = np.exp(sum_log_G)

            elbo = np.sum(self.aa * eps)
            elbo += -np.sum(self.ww * np.exp(eps))
            FthG = F_prod * bar_theta * G_prod
            elbo += -np.sum(FthG)

            # grad = grad(elbo)
            grad = self.aa - self.ww * np.exp(eps)
            grad -= (FthG[:, np.newaxis] * self.Xframe).groupby(uu).sum().values.T

            # minimization objective is log(-elbo)
            return np.log(-elbo), grad.ravel() / elbo

        return minfun

    def _make_approximate_minfun(self):
        """
        Factory function that returns a function to be minimized.
        This version uses an approximate minimization objective.
        """
        uu = self.Nframe['unit']
        tt = self.Nframe['time']
        if self.overdispersion:
            bar_theta = self.omega / self.zeta
        else:
            bar_theta = 1
        sum_log_F_prod = np.sum(np.log(self.F_prod()[tt, uu]))
        sum_log_bar_theta = np.sum(np.log(bar_theta))
        X_sufficient = self.Xframe.groupby(self.Nframe['unit']).sum().values.T
        log_Fth = sum_log_F_prod + sum_log_bar_theta

        def minfun(epsilon): 
            """
            This is the portion of the evidence lower bound that depends on 
            the b parameter. eps = log(a/b)
            """
            eps = epsilon.reshape(self.J, self.U)
            sum_log_G = np.sum(eps[:, uu].T * self.Xframe.values, axis=1)
            FthG = np.exp(np.sum(log_Fth + sum_log_G))

            elbo = np.sum(self.aa * eps)
            elbo += -np.sum(self.ww * np.exp(eps))
            elbo += -FthG

            # grad = grad(elbo)
            grad = self.aa - self.ww * np.exp(eps)
            grad += -X_sufficient * FthG

            # minimization objective is log(-elbo)
            return np.log(-elbo), grad.ravel() / elbo

        return minfun

    def update_theta(self):
        """
        Update parameters corresponding to overdispersion for firing rates.
        """
        N = self.Nframe
        uu = N['unit']
        tt = N['time']
        self.omega = N['count'] + self.ss[uu]
        self.zeta = self.F_prod()[tt, uu] * self.G_prod() + self.rr[uu]

        return self

    def update_z(self, k):
        """
        Update estimates of hidden states for given chain, along with
        two-slice marginals.
        """
        eta = self.calc_log_evidence(k)
        logA = self.calc_log_A()[..., k]
        logpi = self.calc_log_pi()[..., k]

        # do forward-backward inference and assign results
        post, logZ, Xi = fb_infer(np.exp(logA), np.exp(logpi), np.exp(eta))
        self.xi[:, k] = post[:, 1]
        self.logZ[k] = logZ
        self.Xi[:, k] = Xi

        self.F_prod(k, update=True)
        emission_piece = np.sum(post * eta)
        initial_piece = np.sum(post[0] * logpi)
        transition_piece = np.sum(Xi * logA)
        self.logq[k] = emission_piece + initial_piece + transition_piece
        self.H = -self.logq + self.logZ
        return self

    def update_A(self, k):
        """
        Update Markov chain variational parameters for given chain.
        """
        Xibar = np.sum(self.Xi[:, k], axis=0)

        self.gamma1[:, k] = self.nu1[:, k] + Xibar[1] 
        self.gamma2[:, k] = self.nu2[:, k] + Xibar[0]

        return self

    def update_pi(self, k):
        """
        Update Markov chain variational parameters for given chain.
        """
        self.delta1[k] = self.rho1[k] + self.xi[0, k]
        self.delta2[k] = self.rho2[k] + 1 - self.xi[0, k]

        return self

    def iterate(self, verbosity=0, keeplog=False, excluded_iters=[]):
        """
        Do one iteration of variational inference, updating each chain in turn.
        verbosity is a verbosity level:
            0: no print to screen
            1: print L value on each iteration
            2: print L value each update during each iteration
        keeplog = True does internal logging for debugging; values are kept in
            the dict self.log
        excluded_iters is a list of variables to exclude from updating
        """
        doprint = verbosity > 1 
        calc_L = doprint or keeplog
        
        # M step
        if not 'lambda_hypers' in excluded_iters:
            self.update_lambda_mean_hypers()
            self.update_lambda_var_hypers()
            if calc_L:
                Lval = self.L(keeplog=keeplog) 
            if doprint:
                print "chain : updated lambda priors: L = {}".format(Lval)

        for k in xrange(self.K):
            if not 'lambda' in excluded_iters:
                self.update_lambda(k)
                if calc_L:
                    Lval = self.L(keeplog=keeplog) 
                if doprint:
                    print "chain {}: updated lambda: L = {}".format(k, Lval)

            if not 'A' in excluded_iters:
                self.update_A(k)
                if calc_L:
                    Lval = self.L(keeplog=keeplog) 
                if doprint:
                    print "chain {}: updated A: L = {}".format(k, Lval)

            if not 'pi' in excluded_iters:
                self.update_pi(k)
                if calc_L:
                    Lval = self.L(keeplog=keeplog) 
                if doprint:
                    print "chain {}: updated pi: L = {}".format(k, Lval)

        if self.regressors and not 'upsilon' in excluded_iters:
            # if updates are approximate, we may not increase the objective
            # if so, run again
            if self.updater == 'approximate':
                oldL = self.L()
                self.update_upsilon()
                delta = ((self.L() - oldL) / np.abs(oldL))
                if not ((delta > 0) | np.isclose(delta, 0)):
                    print "Upsilon update did not increase objective. Trying exact mode."
                    self.updater = 'exact'
                    self.bb = self.bb_old
                    self.G_prod(update=True)
                    self.update_upsilon()
                    self.updater = 'approximate'
            else:
                self.update_upsilon()

            if calc_L:
                Lval = self.L(keeplog=keeplog) 
            if doprint:
                print "chain  : updated upsilon: L = {}".format(Lval)

        if self.overdispersion and not 'theta' in excluded_iters:
            self.update_theta()
            if calc_L:
                Lval = self.L(keeplog=keeplog) 
            if doprint:
                print "chain  : updated theta: L = {}".format(Lval)

        # E step        
        for k in xrange(self.K):
            if not 'z' in excluded_iters:
                if not (k == 0 and self.include_baseline):
                    self.update_z(k)
                    if calc_L:
                        Lval = self.L(keeplog=keeplog) 
                    if doprint:
                        print "chain {}: updated z: L = {}".format(k, Lval)

    def do_inference(self, verbosity=0, tol=1e-3, keeplog=False, 
        maxiter=np.inf, delayed_iters=[]):
        """
        Perform variational inference by minimizing free energy.
        delayed_iters is a dict of variable names that should not be
        iterated over in the early going; once the algorithm has converged,
        these variables are added in and inference run a second time
        """
        self.Lvalues.append(self.L())
        delta = 1
        idx = 0

        while np.abs(delta) > tol and idx < maxiter:
            if verbosity > 0:
                print "Iteration {}: L = {}".format(idx, self.Lvalues[-1])
                print "delta = " + str(delta)

            self.iterate(verbosity=verbosity, keeplog=keeplog)
            self.Lvalues.append(self.L())

            delta = ((self.Lvalues[-1] - self.Lvalues[-2]) / 
                np.abs(self.Lvalues[-1]))
            assert((delta > 0) | np.isclose(delta, 0))
            idx += 1 

        # now redo inference, this time including all variables that 
        # were delayed
        if len(delayed_iters) > 0:
            print "Initial optimization done, adding {}".format(', '.join(delayed_iters))
            self.do_inference(verbosity=verbosity, tol=tol, keeplog=keeplog,
                maxiter=maxiter, delayed_iters=[])






