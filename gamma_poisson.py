from __future__ import division
import numpy as np
import pandas as pd
from scipy.special import digamma, gammaln, betaln
import pdb

def fb_infer(A, pi0, psi):
    """
    Implement the forward-backward inference algorithm.
    A is a matrix of transition probabilities that acts to the right:
    new_state = A * old_state, so that columns of A sum to 1
    psi is the vector of evidence: p(y_t|z_t); it does not need to be
    normalized, but the lack of normalization will be reflected in logZ
    such that the end result using the given psi will be properly normalized
    when using the returned value of Z
    """
    T = psi.shape[0]
    M = A.shape[0]

    # initialize empty variables
    alpha = np.empty((T, M))  # p(z_t|y_{1:T})
    beta = np.empty((T, M))  # p(y_{t+1:T}|z_t) (unnormalized)
    gamma = np.empty((T, M))  # p(z_t|y_{1:T}) (posterior)
    logZ = np.empty(T)  # log partition function

    # initialize
    alpha[0, :] = pi0
    beta[-1, :] = 1
    logZ[0] = 0
    
    # forwards
    for t in xrange(1, T):
        a = psi[t, :] * (A.dot(alpha[t - 1, :]))
        logZ[t] = np.log(np.sum(a))
        alpha[t, :] = a / np.sum(a)
        
    # backwards
    for t in xrange(T - 1, 0, -1):
        b = A.T.dot(beta[t, :] * psi[t, :])
        beta[t - 1, :] = b / np.sum(b)
        
    # posterior
    gamma = alpha * beta
    gamma = gamma / np.sum(gamma, 1, keepdims=True)
    
    # two-slice marginal matrix: xi = p(z_{t+1}, z_t|y_{1:T})
    # for t = 1:T
    beta_shift = np.expand_dims(np.roll(beta * psi, shift=-1, axis=0), 2)

    # take outer product; make sure t axis on alpha < T
    # and t+1 axis on bp > 0
    Xi = beta_shift[0:(T - 1)] * alpha[1:, np.newaxis, :]

    #normalize
    Xi = Xi / np.sum(Xi, axis=(1, 2), keepdims=True)

    if np.any(np.isnan(gamma)):
        raise ValueError('NaNs appear in posterior')

    return gamma, np.sum(logZ), Xi

class GPModel:
    """
    This class represents and fits a Gamma-Poisson model via variational
    inference. Variables are as follows:
    T: number of (discrete) times
    dt: time difference between observations
    U: number of observation units
    K: number of latent categories to fit

    N: T x U array of count observations
    lam: K x U array of _multiplicative_ effects; lam[0] is baseline
    z: T x K array of latent states in the Markov chain (0 or 1)
    A: 2 x 2 x K array of column-stochastic Markov transition matrices (one
        per chain)
    pi0: 2 x K array of initial chain state probabilities

    Model is defined as:
    N_{tu}|z ~ Poisson(prod_k mu_{ku}^z_{tk})
    lam_{ku}|z ~ Gamma(alpha_{ku}, beta_{ku})
    A(k)_{i -> 1} ~ Beta(gamma1_{ik}, gamma2_{ik})
    pi0(k)_1 ~ Beta(delta1_k, delta2_k)

    Priors are:
    lam_{ku}|z ~ Gamma(cc_{ku}, dd_{ku})
    A(k)_{i -> 1} ~ Beta(nu1_{ik}, nu2_{ik})
    pi0(k)_1 ~ Beta(rho1_k, rho2_k)

    Derived parameters:
    xi_{tk} = E[z_{tk}]
    Xi_{t,k}[j, i] = p(z_{t+1, k}=j, z_{tk}=1)
    """
    def __init__(self, T, K, U, dt, include_baseline=False):
        """
        Set up basic constants for the model. 
        """
        self.T = T
        self.K = K
        self.U = U
        self.dt = dt
        self.include_baseline = include_baseline
        self.prior_pars = ({'cc': (K, U), 'dd': (K, U), 'nu1': (2, K), 
            'nu2': (2, K), 'rho1': (K,), 'rho2': (K,)})
        self.variational_pars = ({'alpha': (K, U), 
            'beta': (K, U), 'gamma1': (2, K), 'gamma2': (2, K), 
            'delta1': (K,), 'delta2': (K,), 'xi': (T, K), 
            'Xi': (T - 1, K, 2, 2), 'eta': (T, K), 'kappa': (K,)})
        self.log = []  # for debugging
        self.Lvalues = []  # for recording value of optimization objective

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

        # normalize Xi
        self.Xi = self.Xi / np.sum(self.Xi, axis=(-1, -2), keepdims=True)

        # make sure baseline category turned on
        if self.include_baseline:
            self.xi[:, 0] = 1

            # ensure p(z_{t+1}=1, z_t=1) = 1
            self.Xi[:, 0] = 0
            self.Xi[:, 0, 1, 1] = 1

        return self

    def set_data(self, Nframe):
        """
        Nframe is a dataframe containing columns unit, movie, frame, and 
        count. 
        """

        # make an array of all spikes for a given time within movie and 
        # unit; this only has to be done once
        countframe = Nframe.groupby(['movie', 'frame', 'unit']).sum().unstack(level=2)
        countarr = countframe.values
        self.N = np.ma.masked_where(np.isnan(countarr), countarr).astype('int')

        # make a dataframe linking movie and frame to a unique time index
        self.t_index = Nframe.drop(['unit', 'count'], axis=1).drop_duplicates().reset_index(drop=True)
        self.t_index.index.name = 'time'
        self.t_index = self.t_index.reset_index()

        # make a frame of presentations linked to time index
        self.Nframe = pd.merge(self.t_index, Nframe).drop(['movie', 'frame'], axis=1)

        # make a masked array counting the number of observations of each 
        # (time, unit) pair
        Nobs = self.Nframe.groupby(['time', 'unit']).count().unstack()
        self.Nobs = np.ma.masked_where(np.isnan(Nobs), Nobs).astype('int')

        return self

    @staticmethod
    def F_prod(z, w, log=False, exclude=True):
        """
        Given z (T x K) and w (K x U), returns the product
        prod_{j neq k} (1 - z_{tj} + z_{tj} * w_{ju})
        log = True returns the log of the result
        exclude = True returns the product over all k
        as a result, it is T x U instead of T x K x U
        """
        zz = z[..., np.newaxis]
        vv = 1 - zz + zz * w
        dd = np.sum(np.log(vv), 1, keepdims=exclude)

        if exclude:
            log_ans =  dd - np.log(vv)
        else:
            log_ans = dd

        if log:
            return log_ans
        else:
            return np.exp(log_ans)

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

    def calc_log_pi0(self):
        """
        Calculate E[log pi0] with pi0 the Markov chain initial state 
        probability. Return exp of this, which is the parameter value to
        be used in calculating updates to the latent states. Note that this 
        does NOT return a probability (does not sum to one). Cf. Beal 2003.
        """ 
        # as per Beal's thesis, these are subadditive, but this is 
        # compensated for by normalization in fb algorithm
        # necessary to correctly calculate logZ
        log_pi0 = np.empty((2, self.K))
        log_pi0[1] = digamma(self.delta1) - digamma(self.delta1 + self.delta2)
        log_pi0[0] = digamma(self.delta2) - digamma(self.delta1 + self.delta2)
        return log_pi0

    def calculate_evidence(self, k):
        """
        Calculate emission probabilities for use in forward-backward 
        algorithm. These are not properly normalized, but that will
        be compensated for in the logZ value returned by fb_infer.
        """
        logpsi = np.empty((self.T, 2))
        bar_log_lambda = digamma(self.alpha) - np.log(self.beta)
        bar_lambda = self.alpha / self.beta
        Fk = self.F_prod(self.xi, bar_lambda)[:, k, :]

        # need to account for multiple observations of same frame by same
        # unit, so use Nframe
        N = self.Nframe.copy()
        nn = N['count']
        tt = N['time']
        uu = N['unit'] - 1
        N['lam0'] = -Fk[tt, uu] 
        N['lam1'] = (nn * bar_log_lambda[k, uu] + 
            Fk[tt, uu] * bar_lambda[k, uu])

        logpsi = N.groupby('time').sum()[['lam0', 'lam1']].values

        return np.exp(logpsi)

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
        bar_log_lambda = digamma(self.alpha) - np.log(self.beta)

        L = [] 
        ############### E[log (p(pi) / q(pi))] #############
        logpi0 = self.calc_log_pi0()
        L.append(np.sum((self.rho1 - 1) * logpi0[1] + (self.rho2 - 1) * logpi0[0]))
        H_pi0 = self.H_beta(self.delta1, self.delta2)
        L.append(np.sum(H_pi0))

        ############### E[log (p(A) / q(A))] #############
        logA = self.calc_log_A()
        L.append(np.sum((self.nu1 - 1) * logA[1] + (self.nu2 - 1) * logA[0]))
        H_A = self.H_beta(self.gamma1, self.gamma2)
        L.append(np.sum(H_A))

        ############### E[log (p(lambda) / q(lambda))] #############
        L.append(np.sum((self.cc - 1) * bar_log_lambda))
        L.append(-np.sum(self.dd * (self.alpha / self.beta)))
        H_lambda = self.H_gamma(self.alpha, self.beta)
        L.append(np.sum(H_lambda))

        ############### E[log (p(N, z|A, pi, lambda) / q(z))] #############
        L.append(np.sum(self.N[:, np.newaxis, :] * self.xi[..., np.newaxis] * bar_log_lambda[np.newaxis, ...]))
        L.append(-np.sum(self.Nobs * self.F_prod(self.xi, self.alpha / self.beta, exclude=False)))
        logpi0 = self.calc_log_pi0()
        L.append(np.sum((1 - self.xi[0]) * logpi0[0] + self.xi[0] * logpi0[1]))
        logA = self.calc_log_A()
        L.append(np.sum(self.Xi * logA.transpose((2, 0, 1))))
        L.append(-np.sum(self.eta))
        L.append(-np.sum(self.kappa))

        ############### log Z #############
        L.append(np.sum(self.logZ))

        if keeplog:
            self.log.append(L)

        return np.sum(L)

    def update_lambda(self, k):
        """
        Update parameters corresponding to emission parameters for 
        each Markov chain.
        """
        Nz = np.sum(self.N[:, np.newaxis, :] * self.xi[:, :, np.newaxis], axis=0)
        Fz = self.F_prod(self.xi, self.alpha / self.beta) * self.xi[:, :, np.newaxis]

        self.alpha[k] = (Nz[k] + self.cc[k]).data
        self.beta[k] = np.sum(self.Nobs * Fz[:, k, :], axis=0) + self.dd[k]

        return self

    def update_z(self, k):
        """
        Update estimates of hidden states for given chain, along with
        two-slice marginals.
        """
        psi = self.calculate_evidence(k)
        logpsi = np.log(psi)
        logA = self.calc_log_A()[..., k]
        logpi0 = self.calc_log_pi0()[..., k]

        # do forward-backward inference and assign results
        if k == 0 and self.include_baseline is True:
            pass
        else:
            post, logZ, Xi = fb_infer(np.exp(logA), np.exp(logpi0), psi)
            self.xi[:, k] = post[:, 1]
            self.logZ[k] = logZ
            self.Xi[:, k] = Xi

        self.eta[:, k] = (1 - self.xi[:, k]) * logpsi[:, 0] + self.xi[:, k] * logpsi[:, 1]
        self.kappa[k] = ((1 - self.xi[0, k]) * logpi0[0] + self.xi[0, k] * logpi0[1] +  np.sum(self.Xi[:, k] * logA))
        return self

    def update_A(self, k):
        """
        Update Markov chain variational parameters for given chain.
        """
        Xibar = np.sum(self.Xi[:, k], axis=0)

        self.gamma1[:, k] = self.nu1[:, k] + Xibar[1] 
        self.gamma2[:, k] = self.nu2[:, k] + Xibar[0]

        return self

    def update_pi0(self, k):
        """
        Update Markov chain variational parameters for given chain.
        """
        self.delta1[k] = self.rho1[k] + self.xi[0, k]
        self.delta2[k] = self.rho2[k] + 1 - self.xi[0, k]

        return self

    def iterate(self, silent=True, keeplog=False):
        """
        Do one iteration of variational inference, updating each chain in turn.
        """
        # M step
        for k in xrange(self.K):
            self.update_lambda(k)
            if (not silent) or keeplog:
                Lval = self.L(keeplog=keeplog) 
            if not silent:
                print "chain {}: updated lambda: L = {}".format(k, Lval)

            self.update_A(k)
            if (not silent) or keeplog:
                Lval = self.L(keeplog=keeplog) 
            if not silent:
                print "chain {}: updated A: L = {}".format(k, Lval)

            self.update_pi0(k)
            if (not silent) or keeplog:
                Lval = self.L(keeplog=keeplog) 
            if not silent:
                print "chain {}: updated pi0: L = {}".format(k, Lval)

        # E step        
        for k in xrange(self.K):
            self.update_z(k)
            if (not silent) or keeplog:
                Lval = self.L(keeplog=keeplog) 
            if not silent:
                print "chain {}: updated z: L = {}".format(k, Lval)

    def do_inference(self, silent=True, tol=1e-3, keeplog=False, 
        maxiter=np.inf):
        """
        Perform variational inference by minimizing free energy.
        """
        self.Lvalues.append(self.L())
        delta = 1
        idx = 0

        while np.abs(delta) > tol and idx < maxiter:
            if not silent:
                print "Iteration {}: L = {}".format(idx, self.Lvalues[-1])
                print "delta = " + str(delta)

            self.iterate(silent=silent, keeplog=keeplog)
            self.Lvalues.append(self.L())

            delta = ((self.Lvalues[-1] - self.Lvalues[-2]) / 
                np.abs(self.Lvalues[-1]))
            idx += 1 






