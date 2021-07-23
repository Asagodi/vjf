from itertools import zip_longest
from typing import Tuple, Sequence, Union

import torch
from torch import Tensor
from torch.nn import Module, Linear, functional, Parameter
from torch.optim import Adam
from torch.optim.lr_scheduler import ExponentialLR
from tqdm import trange

from .functional import gaussian_entropy as entropy
from .module import bLinReg, RBF
from .recognition import DiagonalGaussian, Recognition
from .util import reparametrize


class GaussianLikelihood(Module):
    """
    Gaussian likelihood
    """

    def __init__(self):
        super().__init__()
        self.register_parameter('logvar', Parameter(torch.zeros(1)))

    def loss(self, eta: Tensor, target: Tensor) -> Tensor:
        """
        :param eta: pre inverse link
        :param target: observation
        :return:
        """
        mse = functional.mse_loss(eta, target, reduction='none')
        assert mse.ndim == 2
        p = torch.exp(-self.logvar)
        nll = .5 * (mse * p + self.logvar)
        return nll.sum(-1).mean()


class PoissonLikelihood(Module):
    """
    Poisson likelihood
    """

    def __init__(self):
        super().__init__()

    @staticmethod
    def loss(eta: Tensor, target: Tensor) -> Tensor:
        """
        :param eta: pre inverse link
        :param target: observation
        :return:
        """
        nll = functional.poisson_nll_loss(eta, target, log_input=True, reduction='none')
        assert nll.ndim == 2
        return nll.sum(-1).mean()


def detach(q: DiagonalGaussian) -> DiagonalGaussian:
    mean, logvar = q
    return DiagonalGaussian(mean.detach(), logvar.detach())


class VJF(Module):
    def __init__(self, ydim: int, xdim: int, likelihood: Module, transition: Module, recognition: Module):
        """
        :param likelihood: GLM likelihood, Gaussian or Poisson
        :param transition: f(x[t-1], u[t]) -> x[t]
        :param recognition: y[t], f(x[t-1], u[t]) -> x[t]
        """
        super().__init__()
        self.add_module('likelihood', likelihood)
        self.add_module('transition', transition)
        self.add_module('recognition', recognition)
        self.add_module('decoder', Linear(xdim, ydim))

        self.register_parameter('mean', Parameter(torch.zeros(xdim)))
        self.register_parameter('logvar', Parameter(torch.zeros(xdim)))

        self.optimizer = Adam(self.parameters())
        self.scheduler = ExponentialLR(self.optimizer, 0.95)  # TODO: argument gamma

    def prior(self, y: Tensor) -> DiagonalGaussian:
        assert y.ndim == 2
        n_batch = y.shape[0]
        mean = torch.atleast_2d(self.mean)
        logvar = torch.atleast_2d(self.logvar)

        if mean.shape[0] == 1:
            mean = mean.tile((n_batch, 1))
        if logvar.shape[0] == 1:
            logvar = logvar.tile((n_batch, 1))

        assert mean.size(0) == n_batch and logvar.size(0) == n_batch

        return DiagonalGaussian(mean, logvar)

    def forward(self, y: Tensor, qs: DiagonalGaussian, u: Tensor = None) -> Tuple:
        """
        :param y: new observation
        :param qs: posterior before new observation
        :param u: input, None if autonomous
        :return:
            pt: prediction before observation
            qt: posterior after observation
        """
        # encode
        qs = detach(qs)
        xs = reparametrize(qs)
        pt = self.transition(xs, u)
        # print(torch.linalg.norm(xs - pt).item())

        y = torch.atleast_2d(y)
        qt = self.recognition(y, pt)

        # decode
        xt = reparametrize(qt)
        py = self.decoder(xt)

        return xs, pt, qt, xt, py

    def loss(self, y: Tensor, xs: Tensor, pt: Tensor, qt: DiagonalGaussian, xt: Tensor, py: Tensor,
             components: bool = False, full: bool = False) -> Union[Tensor, Tuple]:
        if full:
            raise NotImplementedError

        # recon
        l_recon = self.likelihood.loss(py, y)
        # dynamics
        l_dynamics = self.transition.loss(pt, xt)
        # entropy
        h = entropy(qt)

        loss = l_recon - h + l_dynamics

        # print(l_recon.item(), l_dynamics.item(), h.item())

        if components:
            return loss, -l_recon, -l_dynamics, h
        else:
            return loss

    @torch.no_grad()
    def update(self, y: Tensor, xs: Tensor, pt: Tensor, qt: DiagonalGaussian, xt: Tensor, py: Tensor):
        # non gradient
        self.transition.update(xs, xt)

    def filter(self, y: Tensor, u: Tensor = None, qs: DiagonalGaussian = None, *, update: bool = True):
        """
        Filter a step or a sequence
        :param y: observation, assumed axis order (time, batch, dim). missing axis will be prepended.
        :param u: control
        :param qs: previos posterior. use prior if None, otherwise detached.
        :param update: flag to learn the parameters
        :return:
            qt: posterior
            loss: negative eblo
        """
        y = torch.as_tensor(y, dtype=torch.get_default_dtype())
        y = torch.atleast_2d(y)  # (batch, dim)
        if u is not None:
            u = torch.as_tensor(u, dtype=torch.get_default_dtype())
            u = torch.atleast_2d(u)

        if qs is None:
            qs = self.prior(y)
        else:
            detach(qs)

        xs, pt, qt, xt, py = self.forward(y, qs, u)
        loss = self.loss(y, xs, pt, qt, xt, py)
        if update:
            self.optimizer.zero_grad()
            loss.backward()  # accumulate grad if not trained
            self.optimizer.step()
            # self.update(y, xs, pt, qt, xt, py)  # non-gradient step

        return qt, loss

    def fit(self, y, u=None, *, max_iter=1):
        """offline"""
        y = torch.as_tensor(y)
        y = torch.atleast_2d(y)
        if u is None:
            u = [None]
        else:
            u = torch.as_tensor(u)

        with trange(max_iter) as progress:
            for i in progress:
                self.optimizer.zero_grad()

                # collections
                q_seq = []  # maybe deque is better than list?
                losses = []

                q = None  # use prior
                for yt, ut in zip_longest(y, u):
                    q, loss = self.filter(yt, ut, q, update=False)
                    losses.append(loss)
                    q_seq.append(q)
                total_loss = sum(losses) / len(losses)
                total_loss.backward()
                self.optimizer.step()

                progress.set_postfix({'Loss': total_loss.item()})

        return q_seq

    @classmethod
    def make_model(cls, ydim: int, xdim: int, udim: int, n_rbf: int, hidden_sizes: Sequence[int],
                   likelihood: str = 'poisson'):
        if likelihood.lower() == 'poisson':
            likelihood = PoissonLikelihood()
        elif likelihood.lower() == 'gaussian':
            likelihood = GaussianLikelihood()

        model = VJF(ydim, xdim, likelihood, RBFLDS(n_rbf, xdim, udim), Recognition(ydim, xdim, hidden_sizes))
        return model


class RBFLDS(Module):
    def __init__(self, n_rbf: int, xdim: int, udim: int):
        super().__init__()
        self.add_module('linreg', bLinReg(RBF(xdim + udim, n_rbf), xdim))
        self.register_parameter('logvar', Parameter(torch.ones(1), requires_grad=False))  # act like a regularizer

    def forward(self, x: Tensor, u: Tensor = None, sampling=False) -> Tensor:
        if u is None:
            xu = x
        else:
            u = torch.atleast_2d(u)
            xu = torch.cat((x, u), dim=-1)

        return x + self.linreg(xu, sampling=sampling)  # model dx
        # return self.linreg(xu, sampling=sampling)  # model f(x)

    def simulate(self, x0: Tensor, step=1) -> Tensor:
        x = torch.empty(step + 1, *x0.shape)
        x[0] = x0
        s = torch.exp(.5 * self.logvar)

        for t in range(step):
            x[t + 1] = self.forward(x[t], sampling=True)
            x[t + 1] = x[t + 1] + torch.randn_like(x[t+1]) * s

        return x

    @torch.no_grad()
    def update(self, xs: Tensor, xt: Tensor):
        self.linreg.update(xs, xt - xs, torch.exp(-self.logvar))  # model dx
        # self.linreg.update(xs, xt, torch.exp(-self.logvar))
        # self.logvar *= 0.99

    def loss(self, pt: Tensor, xt: Tensor) -> Tensor:
        mse = functional.mse_loss(pt, xt, reduction='none')
        p = torch.exp(-self.logvar)  # precision
        nll = .5 * (mse * p + self.logvar)
        assert nll.ndim == 2
        return nll.sum(-1).mean()
