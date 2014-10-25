from __future__ import division
import numpy as np
import scipy.stats as stats
from scipy.special import digamma, gammaln, betaln
import pdb

def calculate_observation_probs(y, pars, rate_min=1e-200):
    """
    Calculates the probability of the observations given the hidden state: 
    p(y_t|z_t, pars). Calculates on the log scale to avoid overflow/underflow 
    issues.
    """
    T = y.shape[0]  # number of observations
    M = pars.shape[-1]  # last index is for values of z
    logpsi = np.empty((T, M))

    # sanitize inputs, since logpmf(lam = 0) = nan
    rates = pars.copy()
    rates[rates == 0] = rate_min

    # Poisson observation model
    # observation matrix is times x units
    # z = 0
    logpsi[:, 0] = np.sum(stats.poisson.logpmf(y, rates[..., 0]), axis=1)

    # z = 1
    logpsi[:, 1] = np.sum(stats.poisson.logpmf(y, rates[..., 1]), axis=1)

    psi = np.exp(logpsi)

    if np.any(np.isnan(psi)):
        raise ValueError('NaNs appear in observation probabilities.')

    return psi


def fb_infer(y, lam, A, pi0):
    """
    Implement the forward-backward inference algorithm.
    y is a times x units matrix of observations (counts)
    lam is a times x units x states array of Poisson rates
    A is a matrix of transition probabilities that acts to the right:
    new_state = A * old_state, so that columns of A sum to 1
    """
    T = y.shape[0]
    M = A.shape[0]

    # initialize empty variables
    alpha = np.empty((T, M))  # p(z_t|y_{1:T})
    beta = np.empty((T, M))  # p(y_{t+1:T}|z_t) (unnormalized)
    gamma = np.empty((T, M))  # p(z_t|y_{1:T}) (posterior)
    logZ = np.empty(T)  # log partition function

    psi = calculate_observation_probs(y, lam)
    
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
    def __init__(self, T, K, U, dt, include_baseline=True):
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
        self.variational_pars = ({'mu': (K, U), 'alpha': (K, U), 
            'beta': (K, U), 'gamma1': (2, K), 'gamma2': (2, K), 
            'delta1': (K,), 'delta2': (K,), 'xi': (T, K), 
            'Xi': (T - 1, K, 2, 2), 'eta': (T, K, U)})
        self.Lvalues = []

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

    def set_data(self, N):
        self.N = N
        return self

    @staticmethod
    def F_prod(z, w, log=False, exclude=True):
        """
        Given z (T x K) and w (K x U), returns the product
        prod_{j neq k} (1 - z_{tj} + z_{tj} * w_{ju})
        log = True returns the log of the result
        exclude = True returns the product over all k;
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

    def calc_A(self):
        """
        Calculate E[log A] with A the Markov chain transition matrix.
        Return exp(E[log A]), which is the parameter value to be used 
        in calculating updates to the latent states.
        """
        log_A_vec = digamma(self.gamma1) - digamma(self.gamma1 + self.gamma2)
        log_A = np.empty((2, 2, self.K))
        log_A[1] = log_A_vec
        A = np.exp(log_A)
        A[0] = 1 - A[1]
        return A

    def calc_pi0(self):
        """
        Calculate E[log pi0] with pi0 the Markov chain initial state 
        probability. Return exp of this, which is the parameter value to
        be used in calculating updates to the latent states.
        """ 
        log_pi0 = np.empty((2, self.K))
        log_pi0[1] = digamma(self.delta1) - digamma(self.delta1 + self.delta2)
        pi0 = np.exp(log_pi0)
        pi0[0] = 1 - pi0[1] 
        return pi0

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
            (alpha + beta -2) * digamma(alpha + beta)) 
        return H

    # def L(self):
    #     """
    #     Calculate E[log p - log q] in the variational approximation.
    #     """

    #     ############### useful expectations ################ 
    #     bar_log_lambda = digamma(self.alpha) - np.log(self.beta)
    #     zbar_mu = (1 - self.xi[..., np.newaxis] + self.xi[..., np.newaxis] * self.mu)
    #     bar_log_A = digamma(self.gamma1) - digamma(self.gamma1 + self.gamma2)
    #     bar_log_Ac = digamma(self.gamma2) - digamma(self.gamma1 + self.gamma2)
    #     bar_log_pi0 = digamma(self.delta1) - digamma(self.delta1 + self.delta2)
    #     bar_log_pi0c = digamma(self.delta2) - digamma(self.delta1 + self.delta2)

    #     ############### E[log p] ################ 

    #     # E[log p] for lambda
    #     L = np.sum(self.N * self.xi.dot(bar_log_lambda))
    #     L -= np.sum(self.F_prod(self.xi, 
    #         self.alpha / self.beta, exclude=False))

    #     # E[log p] for HMM 
    #     log_A = np.log(self.calc_A())
    #     # want tr(log_A^T * Xi); A is (2, 2, K), Xi is (T-1, K, 2, 2)
    #     # thus log_A.T has K index in front, followed by transpose of log(A)
    #     # this matches Xi in all but first index
    #     L += np.sum(log_A.T[np.newaxis, ...] * self.Xi)
    #     log_pi0 = np.log(self.calc_pi0())
    #     xi0_vec = np.vstack([1 - self.xi[0], self.xi[0]])
    #     L += np.sum(xi0_vec * log_pi0)

    #     # lambda priors
    #     L += np.sum((self.cc - 1) * bar_log_lambda)
    #     L -= np.sum(self.dd * (self.alpha / self.beta))

    #     # HMM parameter priors
    #     L += np.sum(self.nu1 * bar_log_A + self.nu2 * bar_log_Ac)
    #     L += np.sum(self.rho1 * bar_log_pi0 + self.rho2 * bar_log_pi0c)

    #     ############### E[log q] ################ 

    #     # mu and eta 
    #     L -= np.sum(self.N * self.xi.dot(np.log(self.mu)))
    #     L -= np.sum(self.N[:, np.newaxis, :] * np.log(self.eta))
    #     L += np.sum(self.eta * zbar_mu) 

    #     # entropy of gamma distributions in q = E[-log q]
    #     H_lambda = self.H_gamma(self.alpha, self.beta)
    #     L += np.sum(H_lambda)

    #     # entropy of beta distributions for HMM params
    #     H_A = self.H_beta(self.gamma1, self.gamma2)
    #     H_pi0 = self.H_beta(self.delta1, self.delta2)
    #     L += np.sum(H_A) + np.sum(H_pi0)

    #     # normalization of the HMM
    #     L += np.sum(self.logZ)

    #     return L

    def L(self):
        """
        Calculate E[log p - log q] in the variational approximation.
        Here, we only want terms that don't cancel identically in the 
        variational updates. This leaves terms involving the approximation
        of the product of chains by a sum over chains.
        """
        ############### useful expectations ################ 
        bar_log_lambda = digamma(self.alpha) - np.log(self.beta)
        zbar_mu = (1 - self.xi[..., np.newaxis] + self.xi[..., np.newaxis] * self.mu)

        ############### E[log p] ################ 
        L = 0
        # E[log p] for lambda
        L += np.sum(self.N * self.xi.dot(bar_log_lambda))
        L -= np.sum(self.F_prod(self.xi, 
            self.alpha / self.beta, exclude=False))

        # lambda priors
        L += np.sum((self.cc - 1) * bar_log_lambda)
        L -= np.sum(self.dd * (self.alpha / self.beta))
        
        # # HMM parameter priors
        # L += np.sum(self.nu1 * bar_log_A + self.nu2 * bar_log_Ac)
        # L += np.sum(self.rho1 * bar_log_pi0 + self.rho2 * bar_log_pi0c)

        ############### E[log q] ################ 

        # mu and eta 
        L -= np.sum(self.N * self.xi.dot(np.log(self.mu)))
        L -= np.sum(self.N[:, np.newaxis, :] * np.log(self.eta))
        L += np.sum(self.eta * zbar_mu) 

        # entropy of gamma distributions in q = E[-log q]
        H_lambda = self.H_gamma(self.alpha, self.beta)
        L += np.sum(H_lambda)

        # entropy of beta distributions for HMM params
        H_A = self.H_beta(self.gamma1, self.gamma2)
        H_pi0 = self.H_beta(self.delta1, self.delta2)
        L += np.sum(H_A) + np.sum(H_pi0)

        # normalization of the HMM
        L += np.sum(self.logZ)

        return L

    def update_chain_rates(self, k):
        """
        Update parameters corresponding to emission parameters for 
        each Markov chain.
        """
        Nz = np.sum(self.N[:, np.newaxis, :] * self.xi[:, :, np.newaxis], axis=0)
        Fz = np.sum(self.F_prod(self.xi, self.alpha / self.beta) * self.xi[:, :, np.newaxis], axis=0)

        self.alpha[k] = Nz[k] + self.cc[k] 
        self.beta[k] = Fz[k] + self.dd[k]

        self.mu[k] = np.exp(digamma(self.alpha[k]) - np.log(self.beta[k]))
        self.eta = self.F_prod(self.xi, self.alpha / self.beta)
        return self

    def update_chain_states(self, k):
        """
        Update estimates of hidden states for given chain, along with
        two-slice marginals.
        """
        # now calculate effective rates for z = 0 and z = 1
        lam = np.empty((self.T, self.U, 2))
        lam[..., 0] = self.eta[:, k, :]
        lam[..., 1] = self.eta[:, k, :] * self.mu[k]        

        # do forward-backward inference and assign results
        if k == 0 and self.include_baseline is True:
            # update logZ[0] = log p(evidence)
            self.logZ[0] = np.sum(stats.poisson.logpmf(self.N, lam[..., 1]))
        else:
            post, logZ, Xi = fb_infer(self.N, lam, self.calc_A()[..., k], self.calc_pi0()[..., k])
            self.xi[:, k] = post[:, 1]
            self.logZ[k] = logZ
            self.Xi[:, k] = Xi

        return self

    def update_chain_pars(self, k):
        """
        Update Markov chain variational parameters for given chain.
        """
        Xibar = np.sum(self.Xi[:, k], axis=0)

        self.gamma1[:, k] = self.nu1[:, k] + Xibar[1] 
        self.gamma2[:, k] = self.nu2[:, k] + Xibar[0]

        self.delta1[k] = self.rho1[k] + self.xi[0, k]
        self.delta2[k] = self.rho2[k] + 1 - self.xi[0, k]

        return self

    def iterate(self):
        """
        Do one iteration of variational inference, updating each chain in turn.
        """
        for k in xrange(self.K):
            self.update_chain_rates(k)
            # print "chain {}: updated chain rates: L = {}".format(k, self.L())
            self.update_chain_pars(k)
            # print "chain {}: updated chain pars: L = {}".format(k, self.L())
            self.update_chain_states(k)
            # print "chain {}: updated chain states: L = {}".format(k, self.L())

        print "L = {}".format(self.L())

    def do_inference(self, silent=False, tol=1e-3):
        """
        Perform variational inference by minimizing free energy.
        """
        self.Lvalues.append(self.L())
        delta = 1
        idx = 0

        while np.abs(delta) > tol:
            if not silent:
                print "Iteration {}: L = {}".format(idx, self.Lvalues[-1])
                print "delta = " + str(delta)

            self.iterate()
            self.Lvalues.append(self.L())

            delta = ((self.Lvalues[-1] - self.Lvalues[-2]) / 
                self.Lvalues[-1])
            idx += 1 






