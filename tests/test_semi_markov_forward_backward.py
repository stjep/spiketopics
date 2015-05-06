"""
Tests for Forwards-Backwards inference for semi-Markov model.
"""
from __future__ import division
from nose.tools import assert_equals, assert_true, set_trace
import numpy as np
import scipy.stats as stats
import numpy.testing as npt
import hsmm_forward_backward as fb

class Test_Forwards_Backwards:
    @classmethod
    def setup_hsmm(self):
        np.random.rand(12345)

        self._setup_constants()

        self._make_transition_probs()

        self._make_chain()

        self._make_fb_data()

        self._calc_emission_probs()

        self._make_duration_dist()

    @classmethod
    def _setup_constants(self):
        """
        These variables determine problem size.
        """
        self.T = 500  # times
        self.K = 4  # levels of hidden state
        self.D = 50

    @classmethod
    def _make_transition_probs(self):
            """
            Make a Markov transition matrix and initial state vector.
            Columns of A sum to 1, so A acts to the right.
            """ 
            lo, hi = 1, 20
            rows = []
            for _ in xrange(self.K):
                alpha = stats.randint.rvs(lo, hi, size=self.K)
                row = stats.dirichlet.rvs(alpha)
                rows.append(row)
            self.A = np.vstack(rows).T

            alpha = stats.randint.rvs(lo, hi, size=self.K)
            self.pi = stats.dirichlet.rvs(alpha).squeeze()

    @classmethod
    def _make_chain(self):
        """
        Make a Markov chain by using the transition probabilities.
        """

        # make the chain by evolving each category through time
        chain = np.empty((self.K, self.T), dtype='int')

        # pick pars for duration distribution
        mm = 10 * np.random.rand(self.K) # mean
        ss = 3 * np.random.rand(self.K) # standard deviation

        # initialize
        t = 0
        while t < self.T:
            if t == 0:
                pp = self.pi
            else:
                pp = self.A.dot(chain[:, t - 1])

            # pick a new state
            newstate = np.random.multinomial(1, pp)[:, np.newaxis]
            k = np.argmax(newstate)

            # pick a duration
            d = np.rint(stats.norm.rvs(loc=mm[k], scale=ss[k])).astype('int')
            d = np.min([d, self.T - d])

            # fill in the next d steps of the chain
            chain[:, t:(t+d)] = newstate
            t += d
            
        self.chain = chain
        self.dur_mean = mm
        self.dur_std = ss

    @classmethod
    def _make_fb_data(self):
        mu = 10 * np.random.rand(self.K)
        sig = 2 * np.random.rand(self.K)

        y = stats.norm.rvs(loc=mu.dot(self.chain), 
            scale=sig.dot(self.chain), 
            size=self.T)

        self.y = y
        self.obs_mean = mu
        self.obs_std = sig

    @classmethod
    def _calc_emission_probs(self):
        logpsi = stats.norm.logpdf(self.y[:, np.newaxis], 
            loc=self.obs_mean[np.newaxis, :], 
            scale=self.obs_std[np.newaxis, :])

        self.log_evidence = logpsi

    @classmethod
    def _make_duration_dist(self):
        dvec = np.arange(self.D)
        logpdf = stats.norm.logpdf(dvec[np.newaxis, :], 
            loc=self.obs_mean[:, np.newaxis],
            scale=self.obs_std[:, np.newaxis])

        # normalize
        logpdf -= np.log(np.sum(np.exp(logpdf), axis=1, keepdims=True))

        self.dvec = dvec
        self.logpd = logpdf

if __name__ == '__main__':
    np.random.rand(12345)

    TFB = Test_Forwards_Backwards()
    TFB.setup_hsmm()