import numpy as np
import scipty.stats as stats

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
    psi = np.empty((T, M))  # p(y_t|z_t) (observation model)
    logpsi = np.empty((T, M))
    gamma = np.empty((T, M))  # p(z_t|y_{1:T}) (posterior)
    logZ = np.empty(T)  # log partition function

    # Poisson observation model
    # observation matrix is times x units
    # z = 0
    logpsi[:, 0] = np.sum(stats.poisson.logpmf(y, lam[..., 0]), axis=1)

    # z = 1
    logpsi[:, 1] = np.sum(stats.poisson.logpmf(y, lam[..., 1]), axis=1)
    
    # take care of underflow
    logpsi = logpsi - np.amax(logpsi, 1, keepdims=True)
    psi = np.exp(logpsi)
    
    # take care of trials where the z = 0 rate is 0 but we had spikes
    bad_times = np.any(lam[..., 0] == 0, 1)
    psi[bad_times, 0] = 0
    psi[bad_times, 1] = 1
    
    # initialize
    alpha[0, :] = pi0
    beta[-1, :] = 1
    logZ[0] = 0
    
    # forwards
    for t in xrange(1, T):
        a = psi[t, :] * (A.dot(alpha[t - 1, :]))
        logZ[t] = np.sum(a)
        alpha[t, :] = a / np.sum(a)
        
    # backwards
    for t in xrange(T - 1, 0, -1):
        b = A.T.dot(beta[t, :] * psi[t, :])
        beta[t - 1, :] = b / np.sum(b)
        
    # posterior
    gamma = alpha * beta
    gamma = gamma / np.sum(gamma, 1, keepdims=True)
    
    # two-slice marginal matrix: xi = p(z_{t+1}, z_t|y_{1:T})
    beta_shift = np.expand_dims(np.roll(beta * psi, shift=-1, axis=0), 2)

    # take outer product; make sure t axis on alpha < T
    # and t+1 axis on bp > 0
    Xi = beta_shift[1:] * alpha[:-1, np.newaxis, :]

    #normalize
    Xi = Xi / np.sum(Xi, axis=(1, 2), keepdims=True)

    if np.any(np.isnan(gamma)):
        raise ValueError('NaNs appear in posterior')

    return gamma, np.sum(logZ), Xi
