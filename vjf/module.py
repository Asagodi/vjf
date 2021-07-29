from typing import Union

import torch
from torch import Tensor, linalg
from torch.nn import Parameter, Module, functional

from .functional import rbf
from . import kalman
from .util import symmetric


class RBF(Module):
    """Radial basis functions"""
    def __init__(self, n_dim: int, n_basis: int, intercept: bool = False):
        super().__init__()
        self.n_basis = n_basis
        self.intercept = intercept
        self.register_parameter('centroid', Parameter(torch.rand(n_basis, n_dim) * 4 - 2., requires_grad=False))
        self.register_parameter('logwidth', Parameter(torch.zeros(n_basis), requires_grad=False))

    @property
    def n_feature(self):
        if self.intercept:
            return self.n_basis + 1
        else:
            return self.n_basis

    def forward(self, x: Tensor) -> Tensor:
        output = rbf(x, self.centroid, self.logwidth.exp())
        if self.intercept:
            output = torch.column_stack((torch.ones(output.shape[0]), output))
        return output


class LinearRegression(Module):
    """Bayesian linear regression"""
    def __init__(self, feature: Module, n_output: int):
        super().__init__()
        self.add_module('feature', feature)
        self.n_output = n_output
        # self.bias = torch.zeros(n_outputs)
        self.w_mean = torch.zeros(self.feature.n_feature, n_output)
        # self.w_cov = torch.eye(self.feature.n_feature)
        self.w_chol = torch.eye(self.feature.n_feature)
        self.w_precision = torch.eye(self.feature.n_feature)

    def forward(self, x: Tensor, sampling=True) -> Tensor:
        feat = self.feature(x)
        w = self.w_mean
        if sampling:
            # w = w + torch.randn_like(w).cholesky_solve(self.w_cholesky)  # sampling
            w = w + self.w_chol.mm(torch.randn_like(w))  # sampling
        else:
            pass
            # V = torch.linalg.multi_dot((feat, self.w_cov, feat.t()))
        return functional.linear(feat, w.t())  # do we need the intercept?

    def rls(self, x: Tensor, target: Tensor, v: Union[Tensor, float], diffusion: float = 0.):
        """
        :param x: (sample, dim)
        :param target: (sample, dim)
        :param v: observation noise
        :param diffusion: forgetfulness
        :return:
        """
        eye = torch.eye(self.w_precision.shape[0])
        P = self.w_precision
        L = linalg.cholesky(P + 1 / diffusion * eye)
        H = P.triangular_solve(L, upper=False).solution
        P = P - H.t().mm(H)

        feat = self.feature(x)  # (sample, feature)
        s = torch.sqrt(v)
        scaled_feat = feat / s
        scaled_target = target / s
        g = P.mm(self.w_mean) + scaled_feat.t().mm(scaled_target)  # what's it called, gain?
        # (feature, feature) (feature, output) + (feature, sample) (sample, output) => (feature, output)
        self.w_precision = P + scaled_feat.t().mm(scaled_feat)
        assert torch.allclose(self.w_precision, self.w_precision.t())  # symmetric
        # (feature, feature) + (feature, sample) (sample, feature) => (feature, feature)
        # self.w_precision = .5 * (self.w_precision + self.w_precision.t())  # make sure symmetric
        self.w_mean = g.cholesky_solve(linalg.cholesky(self.w_precision))
        # (feature, feature) (feature, output) => (feature, output)

    @torch.no_grad()
    def kalman(self, x: Tensor, target: Tensor, v: Union[Tensor, float], diffusion: float = 0.):
        """Update weight using Kalman
        w[t] = w[t-1] + Q
        target[t] = f(x[t])'w[t] + v
        f(x) is the features, e.g. RBF
        Q is diffusion
        :param x: model prediction
        :param target: true x
        :param v: noise variance
        :param diffusion: Q = diffusion * I, default=0. (RLS)
        :return:
        """
        assert diffusion >= 0., 'diffusion needs to be non-negative'
        eye = torch.eye(self.w_mean.shape[0])  # identity matrix (feature, feature)

        # Kalman naming:
        # A: transition matrix
        # Q: state noise
        # H: loading matrix
        # R: observation noise
        Q = diffusion * eye
        A = eye  # diffusion
        H = self.feature(x)  # (sample, feature)
        R = torch.eye(H.shape[0]) * v  # (feature, feature)

        yhat, mhat, Vhat = kalman.predict(self.w_mean, self.w_chol, A, Q, H, R)
        self.w_mean, self.w_chol = kalman.update(target, yhat, mhat, Vhat, H, R)
        # self.w_mean, self.w_chol = kalman.joseph_update(target, yhat, mhat, Vhat, H, R)

    @torch.no_grad()
    def reset(self):
        self.w_mean = torch.zeros_like(self.w_mean)
        # self.w_cov = torch.eye(self.feature.n_feature)
        self.w_chol = torch.eye(self.feature.n_feature)
