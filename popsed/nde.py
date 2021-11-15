'''
Neural density estimators, build based on https://github.com/mackelab/sbi/blob/019fde2d61edbf8b4a02e034dc9c056b0d240a5c/sbi/neural_nets/flow.py#L77

But here everything is not conditioned.
'''
import torch
from torch import nn, Tensor, optim

from nflows import flows, transforms
from nflows.nn import nets
from nflows import distributions as distributions_

from sbi.utils.sbiutils import standardizing_transform
from sbi.utils.torchutils import create_alternating_binary_mask

from tqdm import trange
import pickle
import numpy as np

from scipy.linalg import sqrtm
from scipy.special import digamma
from sklearn.neighbors import NearestNeighbors


def build_maf(
    batch_x: Tensor = None,
    z_score_x: bool = True,
    hidden_features: int = 50,
    num_transforms: int = 5,
    embedding_net: nn.Module = nn.Identity(),
    device: str = 'cuda',
    **kwargs,
) -> nn.Module:
    """Builds MAF to describe p(x).

    Args:
        batch_x: Batch of xs, used to infer dimensionality and (optional) z-scoring.
        z_score_x: Whether to z-score xs passing into the network, i.e., whether do normalization.
        hidden_features: Number of hidden features.
        num_transforms: Number of transforms.
        embedding_net: Optional embedding network for y.
        kwargs: Additional arguments that are passed by the build function but are not
            relevant for maf and are therefore ignored.
    Returns:
        Neural network.
    """
    x_numel = batch_x[0].numel()

    if x_numel == 1:
        warn(f"In one-dimensional output space, this flow is limited to Gaussians")

    transform = transforms.CompositeTransform(
        [
            transforms.CompositeTransform(
                [
                    transforms.MaskedAffineAutoregressiveTransform(
                        features=x_numel,
                        hidden_features=hidden_features,
                        num_blocks=2,
                        use_residual_blocks=False,
                        random_mask=False,
                        activation=torch.tanh,
                        dropout_probability=0.0,
                        use_batch_norm=True,
                    ),
                    transforms.RandomPermutation(features=x_numel),
                ]
            )
            for _ in range(num_transforms)
        ]
    )

    if z_score_x:  # normalize the input data
        transform_zx = standardizing_transform(batch_x)
        transform = transforms.CompositeTransform([transform_zx, transform])

    distribution = distributions_.StandardNormal((x_numel,))
    neural_net = flows.Flow(transform, distribution, embedding_net).to(device)

    return neural_net


def build_nsf(
    batch_x: Tensor = None,
    z_score_x: bool = True,
    hidden_features: int = 50,
    num_transforms: int = 5,
    num_bins: int = 10,
    embedding_net: nn.Module = nn.Identity(),
    device: str = 'cuda',
    **kwargs,
) -> nn.Module:
    """Builds NSF to describe p(x).
    Args:
        batch_x: Batch of xs, used to infer dimensionality and (optional) z-scoring.
        z_score_x: Whether to z-score xs passing into the network.
        hidden_features: Number of hidden features.
        num_transforms: Number of transforms.
        num_bins: Number of bins used for the splines.
        embedding_net: Optional embedding network for y.
        kwargs: Additional arguments that are passed by the build function but are not
            relevant for maf and are therefore ignored.
    Returns:
        Neural network.
    """
    x_numel = batch_x[0].numel()

    if x_numel == 1:

        class ContextSplineMap(nn.Module):
            """
            Neural network from `context` to the spline parameters.
            We cannot use the resnet as conditioner to learn each dimension conditioned
            on the other dimensions (because there is only one). Instead, we learn the
            spline parameters directly. In the case of conditinal density estimation,
            we make the spline parameters conditional on the context. This is
            implemented in this class.
            """

            def __init__(
                self,
                in_features: int,
                out_features: int,
                hidden_features: int,
                context_features: int,
            ):
                """
                Initialize neural network that learns to predict spline parameters.
                Args:
                    in_features: Unused since there is no `conditioner` in 1D.
                    out_features: Number of spline parameters.
                    hidden_features: Number of hidden units.
                    context_features: Number of context features.
                """
                super().__init__()
                # `self.hidden_features` is only defined such that nflows can infer
                # a scaling factor for initializations.
                self.hidden_features = hidden_features

                # Use a non-linearity because otherwise, there will be a linear
                # mapping from context features onto distribution parameters.
                self.spline_predictor = nn.Sequential(
                    nn.Linear(context_features, self.hidden_features),
                    nn.ReLU(),
                    nn.Linear(self.hidden_features, self.hidden_features),
                    nn.ReLU(),
                    nn.Linear(self.hidden_features, out_features),
                )

            def __call__(
                self, inputs: Tensor, context: Tensor, *args, **kwargs
            ) -> Tensor:
                """
                Return parameters of the spline given the context.
                Args:
                    inputs: Unused. It would usually be the other dimensions, but in
                        1D, there are no other dimensions.
                    context: Context features.
                Returns:
                    Spline parameters.
                """
                return self.spline_predictor(context)

        def mask_in_layer(i): return tensor([1], dtype=uint8)

        def conditioner(in_features, out_features): return ContextSplineMap(
            in_features, out_features, hidden_features, context_features=None
        )
        if num_transforms > 1:
            warn(
                f"You are using `num_transforms={num_transforms}`. When estimating a "
                f"1D density, you will not get any performance increase by using "
                f"multiple transforms with NSF. We recommend setting "
                f"`num_transforms=1` for faster training (see also 'Change "
                f"hyperparameters of density esitmators' here: "
                f"https://www.mackelab.org/sbi/tutorial/04_density_estimators/)."
            )

    else:
        def mask_in_layer(i): return create_alternating_binary_mask(
            features=x_numel, even=(i % 2 == 0)
        )

        def conditioner(in_features, out_features): return nets.ResidualNet(
            in_features=in_features,
            out_features=out_features,
            hidden_features=hidden_features,
            context_features=None,
            num_blocks=2,
            activation=nn.ReLU(),
            dropout_probability=0.0,
            use_batch_norm=False,
        )

    transform = transforms.CompositeTransform(
        [
            transforms.CompositeTransform(
                [
                    transforms.PiecewiseRationalQuadraticCouplingTransform(
                        mask=mask_in_layer(i),
                        transform_net_create_fn=conditioner,
                        num_bins=num_bins,
                        tails="linear",
                        tail_bound=3.0,
                        apply_unconditional_transform=False,
                    ),
                    transforms.LULinear(x_numel, identity_init=True),
                ]
            )
            for i in range(num_transforms)
        ]
    )

    if z_score_x:
        transform_zx = standardizing_transform(batch_x)
        transform = transforms.CompositeTransform([transform_zx, transform])

    distribution = distributions_.StandardNormal((x_numel,))
    neural_net = flows.Flow(transform, distribution, embedding_net).to(device)

    return neural_net


class NeuralDensityEstimator(object):
    """
    Neural density estimator.
    """

    def __init__(
            self,
            normalize: bool = True,
            method: str = "nsf",
            hidden_features: int = 50,
            num_transforms: int = 5,
            num_bins: int = 10,
            embedding_net: nn.Module = nn.Identity(),
            **kwargs):
        """
        Initialize neural density estimator.
        Args:
            normalize: Whether to z-score the data.
            method: Method to use for density estimation, either 'nsf' or 'maf'.
            hidden_features: Number of hidden features.
            num_transforms: Number of transforms.
            num_bins: Number of bins used for the splines.
            embedding_net: Optional embedding network for y.
            kwargs: Additional arguments that are passed by the build function but are not
                relevant for maf and are therefore ignored.
        """
        self.device = torch.device(
            'cuda' if torch.cuda.is_available() else 'cpu')
        self.method = method
        self.hidden_features = hidden_features
        self.num_transforms = num_transforms
        self.num_bins = num_bins  # only works for NSF
        self.normalize = normalize
        self.embedding_net = embedding_net
        self.train_loss_history = []

    def build(self, batch_x: Tensor, optimizer: str = "adam", **kwargs):
        """
        Build the neural density estimator based on input data.
        """
        if not torch.is_tensor(batch_x):
            batch_x = torch.tensor(batch_x, device=self.device)
        self.batch_x = batch_x

        if self.method == "maf":
            self.net = build_maf(
                batch_x=batch_x,
                z_score_x=self.normalize,
                hidden_features=self.hidden_features,
                num_transforms=self.num_transforms,
                embedding_net=self.embedding_net,
                device=self.device,
                **kwargs
            )
        elif self.method == "nsf":
            self.net = build_nsf(
                batch_x=batch_x,
                z_score_x=self.normalize,
                hidden_features=self.hidden_features,
                num_transforms=self.num_transforms,
                num_bins=self.num_bins,
                embedding_net=self.embedding_net,
                device=self.device,
                **kwargs
            )

        self.net.to(self.device)

        if optimizer == "adam":
            self.optimizer = optim.Adam(self.net.parameters())
        else:
            raise ValueError(
                f"Unknown optimizer {optimizer}, only support 'Adam' now.")

    def train(self, n_epochs: int = 2000, display=False, suffix: str = "nde"):
        """
        Train the neural density estimator based on input data.
        """
        min_loss = -19
        patience = 5
        self.best_loss_epoch = 0
        self.net.train()

        for epoch in trange(n_epochs, desc='Training NDE', unit='epochs'):
            self.optimizer.zero_grad()
            loss = -self.net.log_prob(self.batch_x).mean()
            loss.backward()
            self.optimizer.step()
            self.train_loss_history.append(loss.item())

            if loss.item() < min_loss:
                min_loss = loss.item()
                if epoch - self.best_loss_epoch > patience:
                    # Don't save model too frequently
                    self.best_loss_epoch = epoch
                    self.save_model(
                        f'best_loss_model_{suffix}_{self.method}.pkl')

        if min_loss == -18:
            raise Warning('The training might be failed, try more epochs')

        if display:
            self.plot_loss()

    def sample(self, n_samples: int = 1000):
        """
        Sample according to the fitted NDE
        """
        return self.net.sample(n_samples)

    def plot_loss(self):
        import matplotlib.pyplot as plt
        plt.plot(np.array(self.train_loss_history).flatten(), label='Train loss')
        plt.xlabel('Epoch')
        plt.ylabel(r'Loss = $-\sum\log(P)$')

    def save_model(self, filename):
        with open(filename, 'wb') as f:
            pickle.dump(self, f)

    def load_model(self, filename):
        with open(filename, 'rb') as f:
            self = pickle.load(f)


def diff_KL_w2009_eq29(X, Y, silent=True, frac=1):
    """
    This function is not accurate! Just a rough estimation when X and Y are very different! 
    PyTorch/Faiss implementation of the KL divergence from Wang 2009 eq. 29.

    kNN KL divergence estimate using Eq. 29 from Wang et al. (2009). 
    This has some bias reduction applied to it and a correction for 
    epsilon.

    Sources 
    ------- 
    - Q. Wang, S. Kulkarni, & S. Verdu (2009). Divergence Estimation for Multidimensional Densities Via k-Nearest-Neighbor Distances. IEEE Transactions on Information Theory, 55(5), 2392-2405.
    """
    import faiss
    import faiss.contrib.torch_utils

    # if not torch.is_tensor(X):
    #     raise ValueError('The input X must be tensor.')
    # if not torch.is_tensor(Y):
    #     raise ValueError('The input Y must be tensor.')

    assert X.shape[1] == Y.shape[1]
    n, d = X.shape  # X sample size, dimensions
    m = Y.shape[0]  # Y sample size

    # first determine epsilon(i)
    NN_X = faiss.IndexFlatL2(d)   # build the index
    NN_X.add(X)                  # add vectors to the index
    NN_Y = faiss.IndexFlatL2(d)   # build the index
    NN_Y.add(Y)                  # add vectors to the index
    dNN1_XX, _ = NN_X.search(X, int(frac * n))
    dNN1_XX = torch.sqrt(dNN1_XX[:, 1:])

    dNN1_XY, _ = NN_Y.search(X, int(frac * n))
    dNN1_XY = torch.sqrt(dNN1_XY)

    eps = torch.amax(
        torch.cat((dNN1_XX[:, 0:1], dNN1_XY[:, 0:1]), dim=1), 1) * 1.000001
    eps = eps.type(torch.float64)
    if not silent:
        print('  epsilons ', eps)

    # find l_i and k_i, fast now
    l_i = torch.empty(n, dtype=int)
    k_i = torch.empty(n, dtype=int)
    rho_i = torch.empty(n, dtype=float)
    nu_i = torch.empty(n, dtype=float)

    for i, e in enumerate(eps):
        l_i[i] = torch.sum(dNN1_XX[i:i+1] <= e)
        rho_i[i] = dNN1_XX[i:i+1][0][l_i[i] - 1]

        k_i[i] = torch.sum(dNN1_XY[i:i+1] <= e)
        nu_i[i] = dNN1_XY[i:i+1][0][k_i[i] - 1]

    if not silent:
        print('  l_i ', l_i)
        print('  k_i ', k_i)

    assert rho_i.min() >= 0., 'duplicate elements in your chain'

    mask = ~torch.isinf(torch.log(rho_i / nu_i))
    d_corr = d / n * torch.nansum(torch.log(rho_i / nu_i)[mask])

    if not silent:
        print('  first term = %f' % d_corr)
    digamma_term = torch.sum(digamma(l_i) - digamma(k_i)) / n
    if not silent:
        print('  digamma term = %f' % digamma_term)

    # l_i, k_i, rho_i, nu_i
    return d_corr + digamma_term + np.log(float(m)/float(n-1))


def KL_w2009_eq29(X, Y, silent=True):
    """
    PyTorch/Faiss implementation of the KL divergence from Wang 2009 eq. 29.

    kNN KL divergence estimate using Eq. 29 from Wang et al. (2009). 
    This has some bias reduction applied to it and a correction for 
    epsilon.

    Sources 
    ------- 
    - Q. Wang, S. Kulkarni, & S. Verdu (2009). Divergence Estimation for Multidimensional Densities Via k-Nearest-Neighbor Distances. IEEE Transactions on Information Theory, 55(5), 2392-2405.
    """
    import faiss

    if torch.is_tensor(X):
        X = X.cpu().detach().numpy()
    if torch.is_tensor(Y):
        Y = Y.cpu().detach().numpy()

    assert X.shape[1] == Y.shape[1]
    n, d = X.shape  # X sample size, dimensions
    m = Y.shape[0]  # Y sample size

    # first determine epsilon(i)
    NN_X = faiss.IndexFlatL2(d)   # build the index
    NN_X.add(X)                  # add vectors to the index
    NN_Y = faiss.IndexFlatL2(d)   # build the index
    NN_Y.add(Y)                  # add vectors to the index
    dNN1_XX, _ = NN_X.search(X, 2)
    dNN1_XY, _ = NN_Y.search(X, 1)
    eps = np.amax([np.sqrt(dNN1_XX[:, 1]), np.sqrt(
        dNN1_XY[:, 0])], axis=0) * 1.000001
    eps = eps.astype('float64')
    if not silent:
        print('  epsilons ', eps)

    # find l_i and k_i
    l_i = np.empty(n, dtype=int)
    k_i = np.empty(n, dtype=int)
    rho_i = np.empty(n, dtype=float)
    nu_i = np.empty(n, dtype=float)
    for i, e in enumerate(eps):
        _, dist, index = NN_X.range_search(X[i:i+1], e**2)
        l_i[i] = len(index) - 1
        rho_i[i] = np.sqrt(np.max(dist)) if len(index) > 0 else 0

        _, dist, index = NN_Y.range_search(X[i:i+1], e**2)
        k_i[i] = len(index)
        nu_i[i] = np.sqrt(np.max(dist)) if len(index) > 0 else 0

    assert np.min(l_i) > 0
    assert np.min(k_i) > 0
    if not silent:
        print('  l_i ', l_i)
        print('  k_i ', k_i)

    assert rho_i.min() > 0., 'duplicate elements in your chain'

    d_corr = float(d) / float(n) * np.sum(np.log(nu_i/rho_i))
    if not silent:
        print('  first term = %f' % d_corr)
    digamma_term = np.sum(digamma(l_i) - digamma(k_i)) / float(n)
    if not silent:
        print('  digamma term = %f' % digamma_term)
    return d_corr + digamma_term + np.log(float(m)/float(n-1))


def _KL_w2009_eq29(X, Y, silent=True):
    ''' kNN KL divergence estimate using Eq. 29 from Wang et al. (2009). 
    This has some bias reduction applied to it and a correction for 
    epsilon.
    sources 
    ------- 
    - Q. Wang, S. Kulkarni, & S. Verdu (2009). Divergence Estimation for Multidimensional Densities Via k-Nearest-Neighbor Distances. IEEE Transactions on Information Theory, 55(5), 2392-2405.
    '''
    if torch.is_tensor(X):
        X = X.cpu().detach().numpy()
    if torch.is_tensor(Y):
        Y = Y.cpu().detach().numpy()

    assert X.shape[1] == Y.shape[1]
    n, d = X.shape  # X sample size, dimensions
    m = Y.shape[0]  # Y sample size

    # first determine epsilon(i)
    NN_X = NearestNeighbors(n_neighbors=1).fit(X)
    NN_Y = NearestNeighbors(n_neighbors=1).fit(Y)
    dNN1_XX, _ = NN_X.kneighbors(X, n_neighbors=2)
    dNN1_XY, _ = NN_Y.kneighbors(X)
    eps = np.amax([dNN1_XX[:, 1], dNN1_XY[:, 0]], axis=0) * 1.000001
    if not silent:
        print('  epsilons ', eps)

    # find l_i and k_i
    _, i_l = NN_X.radius_neighbors(X, eps)
    _, i_k = NN_Y.radius_neighbors(X, eps)
    l_i = np.array([len(il)-1 for il in i_l])
    k_i = np.array([len(ik) for ik in i_k])
    assert l_i.min() > 0
    assert k_i.min() > 0
    if not silent:
        print('  l_i ', l_i)
        print('  k_i ', k_i)

    rho_i = np.empty(n, dtype=float)
    nu_i = np.empty(n, dtype=float)
    for i in range(n):
        rho_ii, _ = NN_X.kneighbors(np.atleast_2d(X[i]), n_neighbors=l_i[i]+1)
        nu_ii, _ = NN_Y.kneighbors(np.atleast_2d(X[i]), n_neighbors=k_i[i])
        rho_i[i] = rho_ii[0][-1]
        nu_i[i] = nu_ii[0][-1]

    assert rho_i.min() > 0., 'duplicate elements in your chain'

    d_corr = float(d) / float(n) * np.sum(np.log(nu_i/rho_i))
    if not silent:
        print('  first term = %f' % d_corr)
    digamma_term = np.sum(digamma(l_i) - digamma(k_i)) / float(n)
    if not silent:
        print('  digamma term = %f' % digamma_term)
    return d_corr + digamma_term + np.log(float(m)/float(n-1))
