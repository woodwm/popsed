'''
Utility functions. Ref: https://github.com/changhoonhahn/provabgs
'''
import os
import numba
import numpy as np
import scipy.sparse
import scipy.special
from astropy.io import fits
import torch


# --- constants ----
def Lsun():
    return 3.846e33  # erg/s


def parsec():
    return 3.085677581467192e18  # in cm


def to_cgs():  # at 10pc
    lsun = Lsun()
    pc = parsec()
    return lsun / (4.0 * np.pi * (10 * pc)**2)


def c_light():  # AA/s
    return 2.998e18


def jansky_cgs():
    return 1e-23

# --- flux and magnitudes ---


def flux2mag(flux):
    ''' convert flux in nanomaggies to magnitudes
    From https://github.com/changhoonhahn/SEDflow/blob/main/src/sedflow/train.py
    '''
    if torch.is_tensor(flux):
        return 22.5 - 2.5 * torch.log10(flux)
    else:
        return 22.5 - 2.5 * np.log10(flux)


def mag2flux(mag):
    ''' convert magnitudes to flux in nanomaggies
    '''
    return 10**(0.4 * (22.5 - mag))


def sigma_flux2mag(sigma_flux, flux):
    ''' convert sigma_flux to sigma_mag
    '''
    if torch.is_tensor(flux):
        return torch.abs(-2.5 * (sigma_flux) / flux / 2.302585092994046)
    else:
        return np.abs(-2.5 * (sigma_flux) / flux / np.log(10))


def sigma_mag2flux(sigma_mag, mag):
    ''' convert sigma_mag to sigma_flux
    '''
    flux = mag2flux(mag)
    if torch.is_tensor(mag):
        return torch.abs(flux) * torch.abs(-0.4 * 2.302585092994046 * sigma_mag)
    else:
        return np.abs(flux) * np.abs(-0.4 * np.log(10) * sigma_mag)


def split_chunks(lst, n):
    """Yield successive n-sized chunks from lst."""
    for i in range(0, len(lst), n):
        yield lst[i:i + n]


def interp_nan(log_spec):
    def nan_helper(y):
        return np.isnan(y), lambda z: z.nonzero()[0]
    for _spec in log_spec:
        nans, x = nan_helper(_spec)
        _spec[nans] = np.interp(x(nans), x(~nans), _spec[~nans])
    return log_spec

# --- SFH related ---


def tlookback_bin_edges(tage):
    ''' hardcoded log-spaced lookback time bin edges. Bins have 0.1 log10(Gyr)
    widths. See `nb/tlookback_binning.ipynb` for comparison of linear and
    log-spaced binning. With log-space we reproduce spectra constructed from
    high time resolution SFHs more accurately with fewer stellar populations.
    '''
    bin_edges = np.zeros(43)
    bin_edges[1:-1] = 10**(6.05 + 0.1 * np.arange(41) - 9.)
    bin_edges[-1] = 13.8
    if tage is None:
        return bin_edges
    else:
        if np.isscalar(tage):
            tage = np.array([tage])
        return np.hstack([bin_edges[bin_edges < tage.ravel()], tage.ravel()])


def readDESIspec(ffits):
    ''' read DESI spectra fits file

    :params ffits: 
        name of fits file  

    :returns spec:
        dictionary of spectra
    '''
    fitobj = fits.open(ffits)

    spec = {}
    for i_k, k in enumerate(['wave', 'flux', 'ivar']):
        spec[k + '_b'] = fitobj[3 + i_k].data
        spec[k + '_r'] = fitobj[8 + i_k].data
        spec[k + '_z'] = fitobj[13 + i_k].data

    spec['TARGETID'] = fitobj[1].data['TARGETID']
    return spec


# --- the code below is taken from the `desispec` and `redrock` python package.
# I've copied the code over instead of importing it to reduce package
# dependencies.
# https://github.com/desihub/redrock/blob/8e33f642b6d8a762c4d71626fb3aca6377c976d4/py/redrock/rebin.py#L10
# ---
class Resolution(scipy.sparse.dia_matrix):
    """Canonical representation of a resolution matrix.
    Inherits all of the method of scipy.sparse.dia_matrix, including todense()
    for converting to a dense 2D numpy array of matrix elements, most of which
    will be zero (so you generally want to avoid this).
    Args:
        data: Must be in one of the following formats listed below.
    Options:
        offsets: list of diagonals that the data represents.  Only used if
            data is a 2D dense array.
    Raises:
        ValueError: Invalid input for initializing a sparse resolution matrix.
    Data formats:
    1. a scipy.sparse matrix in DIA format with the required diagonals
       (but not necessarily in the canoncial order);
    2. a 2D square numpy arrray (i.e., a dense matrix) whose non-zero
       values beyond default_ndiag will be silently dropped; or
    3. a 2D numpy array[ndiag, nwave] that encodes the sparse diagonal
       values in the same format as scipy.sparse.dia_matrix.data .
    The last format is the one used to store resolution matrices in FITS files.
    """

    def __init__(self, data, offsets=None):

        # - Sanity check on length of offsets
        if offsets is not None:
            if len(offsets) < 3:
                raise ValueError(
                    "Only {} resolution matrix diagonals?  That's probably way too small".format(len(offsets)))
            if len(offsets) > 4 * default_ndiag:
                raise ValueError(
                    "{} resolution matrix diagonals?  That's probably way too big".format(len(offsets)))

        if scipy.sparse.isspmatrix_dia(data):
            # Input is already in DIA format with the required diagonals.
            # We just need to put the diagonals in the correct order.
            diadata, offsets = _sort_and_symmeterize(data.data, data.offsets)
            self.offsets = offsets
            scipy.sparse.dia_matrix.__init__(
                self, (diadata, offsets), data.shape)

        elif isinstance(data, np.ndarray) and data.ndim == 2:
            n1, n2 = data.shape
            if n2 > n1:
                ntotdiag = n1  # - rename for clarity
                if offsets is not None:
                    diadata, offsets = _sort_and_symmeterize(data, offsets)
                    self.offsets = offsets
                    scipy.sparse.dia_matrix.__init__(
                        self, (diadata, offsets), (n2, n2))
                elif ntotdiag % 2 == 0:
                    raise ValueError(
                        "Number of diagonals ({}) should be odd if offsets aren't included".format(ntotdiag))
                else:
                    # - Auto-derive offsets
                    self.offsets = np.arange(
                        ntotdiag // 2, -(ntotdiag // 2) - 1, -1)
                    scipy.sparse.dia_matrix.__init__(
                        self, (data, self.offsets), (n2, n2))
            elif n1 == n2:
                if offsets is None:
                    self.offsets = np.arange(
                        default_ndiag // 2, -(default_ndiag // 2) - 1, -1)
                else:
                    self.offsets = np.sort(offsets)[-1::-1]  # - reverse sort

                sparse_data = np.zeros((len(self.offsets), n1))
                for index, offset in enumerate(self.offsets):
                    where = slice(offset, None) if offset >= 0 else slice(
                        None, offset)
                    sparse_data[index, where] = np.diag(data, offset)
                scipy.sparse.dia_matrix.__init__(
                    self, (sparse_data, self.offsets), (n1, n1))
            else:
                raise ValueError(
                    'Cannot initialize Resolution with array shape (%d,%d)' % (n1, n2))

        # - 1D data: Interpret as Gaussian sigmas in pixel units
        elif isinstance(data, np.ndarray) and data.ndim == 1:
            nwave = len(data)
            rdata = np.empty((default_ndiag, nwave))
            self.offsets = np.arange(
                default_ndiag // 2, -(default_ndiag // 2) - 1, -1)
            for i in range(nwave):
                rdata[:, i] = np.abs(_gauss_pix(self.offsets, sigma=data[i]))

            scipy.sparse.dia_matrix.__init__(
                self, (rdata, self.offsets), (nwave, nwave))

        else:
            raise ValueError('Cannot initialize Resolution from %r' % data)

    def to_fits_array(self):
        """Convert to an array of sparse diagonal values.
        This is the format used to store resolution matrices in FITS files.
        Note that some values in the returned rectangular array do not
        correspond to actual matrix elements since the diagonals get smaller
        as you move away from the central diagonal. As long as you treat this
        array as an opaque representation for FITS I/O, you don't care about
        this. To actually use the matrix, create a Resolution object from the
        fits array first.
        Returns:
            numpy.ndarray: An array of (num_diagonals,nbins) sparse matrix
                element values close to the diagonal.
        """
        return self.data


def centers2edges(centers):
    """Convert bin centers to bin edges, guessing at what you probably meant
    Args:
        centers (array): bin centers,
    Returns:
        array: bin edges, lenth = len(centers) + 1
    """
    centers = np.asarray(centers)
    edges = np.zeros(len(centers) + 1)
    # - Interior edges are just points half way between bin centers
    edges[1:-1] = (centers[0:-1] + centers[1:]) / 2.0
    # - edge edges are extrapolation of interior bin sizes
    edges[0] = centers[0] - (centers[1] - edges[1])
    edges[-1] = centers[-1] + (centers[-1] - edges[-2])

    return edges


@numba.jit(nopython=True, fastmath=True)
def pcaMLP(tt, W0, W1, W2, b0, b1, b2, alphas_, betas_, param_shift, param_scale,
           pca_shift_, pca_scale_, pca_transform_matrix_, n_layers):
    ''' simple MLP implemented in float32 to exploit numba speedup.
    '''
    one = np.float32(1.)
    # forward pass through the network
    layers = (tt - param_shift) / param_scale

    # linear network operation
    act = np.dot(layers, W0) + b0

    # pass through activation function
    layers = ((betas_[0] + (one - betas_[0]) /
              (one + np.exp(-alphas_[0] * act))) * act)

    for i in range(1, n_layers - 1):
        # linear network operation
        act = np.dot(layers, W1[i - 1]) + b1[i - 1]

        # pass through activation function
        layers = ((betas_[i] + (one - betas_[i]) /
                  (one + np.exp(-alphas_[i] * act))) * act)

    # final (linear) layer -> (normalized) PCA coefficients
    layers = np.dot(layers, W2) + b2

    # rescale PCA coefficients, multiply out PCA basis -> normalized spectrum, shift and re-scale spectrum -> output spectrum
    pca = np.dot(layers * pca_scale_ + pca_shift_, pca_transform_matrix_)
    return pca

# This code is purposely written in a very "C-like" way.  The logic
# being that it may help numba optimization and also makes it easier
# if it ever needs to be ported to Cython.  Actually Cython versions
# of this code have already been tested and shown to perform no better
# than numba on Intel haswell and KNL architectures.


@numba.jit
def _trapz_rebin(x, y, edges, results):
    '''
    Numba-friendly version of trapezoidal rebinning
    See redrock.rebin.trapz_rebin() for input descriptions.
    `results` is pre-allocated array of length len(edges)-1 to keep results
    '''
    nbin = len(edges) - 1
    i = 0  # - index counter for output
    j = 0  # - index counter for inputs
    yedge = 0.0
    area = 0.0

    while i < nbin:
        # - Seek next sample beyond bin edge
        while x[j] <= edges[i]:
            j += 1

        # - What is the y value where the interpolation crossed the edge?
        yedge = y[j - 1] + (edges[i] - x[j - 1]) * \
            (y[j] - y[j - 1]) / (x[j] - x[j - 1])

        # - Is this sample inside this bin?
        if x[j] < edges[i + 1]:
            area = 0.5 * (y[j] + yedge) * (x[j] - edges[i])
            results[i] += area

            # - Continue with interior bins
            while x[j + 1] < edges[i + 1]:
                j += 1
                area = 0.5 * (y[j] + y[j - 1]) * (x[j] - x[j - 1])
                results[i] += area

            # - Next sample will be outside this bin; handle upper edge
            yedge = y[j] + (edges[i + 1] - x[j]) * \
                (y[j + 1] - y[j]) / (x[j + 1] - x[j])
            area = 0.5 * (yedge + y[j]) * (edges[i + 1] - x[j])
            results[i] += area

        # - Otherwise the samples span over this bin
        else:
            ylo = y[j] + (edges[i] - x[j]) * \
                (y[j] - y[j - 1]) / (x[j] - x[j - 1])
            yhi = y[j] + (edges[i + 1] - x[j]) * \
                (y[j] - y[j - 1]) / (x[j] - x[j - 1])
            area = 0.5 * (ylo + yhi) * (edges[i + 1] - edges[i])
            results[i] += area

        i += 1

    for i in range(nbin):
        results[i] /= edges[i + 1] - edges[i]

    return


def trapz_rebin(x, y, xnew=None, edges=None):
    """Rebin y(x) flux density using trapezoidal integration between bin edges
    Notes:
        y is interpreted as a density, as is the output, e.g.
        >>> x = np.arange(10)
        >>> y = np.ones(10)
        >>> trapz_rebin(x, y, edges=[0,2,4,6,8])  #- density still 1, not 2
        array([ 1.,  1.,  1.,  1.])
    Args:
        x (array): input x values.
        y (array): input y values.
        edges (array): (optional) new bin edges.
    Returns:
        array: integrated results with len(results) = len(edges)-1
    Raises:
        ValueError: if edges are outside the range of x or if len(x) != len(y)
    """
    if edges is None:
        edges = centers2edges(xnew)
    else:
        edges = np.asarray(edges)

    if edges[0] < x[0] or x[-1] < edges[-1]:
        raise ValueError('edges must be within input x range')

    result = np.zeros(len(edges) - 1, dtype=np.float64)

    _trapz_rebin(x, y, edges, result)

    return result


def betterstep(bins, y, **kwargs):
    """A 'better' version of matplotlib's step function 
    (from https://gist.github.com/dfm/e9d36037e363f04acbc668ec7c408237)

    Given a set of bin edges and bin heights, this plots the thing
    that I wish matplotlib's ``step`` command plotted. All extra
    arguments are passed directly to matplotlib's ``plot`` command.

    Args:
        bins: The bin edges. This should be one element longer than
            the bin heights array ``y``.
        y: The bin heights.
        ax (Optional): The axis where this should be plotted.

    """
    import matplotlib.pyplot as plt
    new_x = [a for row in zip(bins[:-1], bins[1:]) for a in row]
    new_y = [a for row in zip(y, y) for a in row]
    ax = kwargs.pop("ax", plt.gca())
    return ax.plot(new_x, new_y, **kwargs)


def flatten_chain(chain):
    ''' flatten mcmc chain. If chain object is 2D then it assumes it's
    already flattened. 
    '''
    if len(chain.shape) == 2:
        return chain  # already flat

    s = list(chain.shape[1:])
    s[0] = np.prod(chain.shape[:2])
    return chain.reshape(s)


def set_env(project='popsed', name='', data_dir='/scratch/gpfs/jiaxuanl/Data/'):
    import os

    # Master directory
    try:
        data_dir = os.path.join(
            os.getenv('HOME'), data_dir, project, name)
    except:
        raise Exception("Can not recognize this dataset!")

    os.chdir(data_dir)
    return data_dir


def set_matplotlib(style='JL', usetex=True, fontsize=13, figsize=(6, 5), dpi=60):
    '''
    Default matplotlib settings, borrowed from Song Huang. I really like his plotting style.

    Parameters:
        style (str): options are "JL", "SM" (supermongo-like).
    '''

    import matplotlib.pyplot as plt
    from matplotlib.colorbar import Colorbar
    from matplotlib import rcParams
    import popsed

    # Use JL as a template
    pkg_path = popsed.__path__[0]
    if style == 'default':
        plt.style.use(os.path.join(pkg_path, 'mplstyle/default.mplstyle'))
    else:
        plt.style.use(os.path.join(pkg_path, 'mplstyle/JL.mplstyle'))

    rcParams.update({'font.size': fontsize,
                     'figure.figsize': "{0}, {1}".format(figsize[0], figsize[1]),
                     'text.usetex': usetex,
                     'figure.dpi': dpi})

    if style == 'SM':
        rcParams.update({
            "figure.figsize": "6, 6",
            "axes.linewidth": 0.6,
            "xtick.major.width": 0.5,
            "xtick.minor.width": 0.3,
            "ytick.major.width": 0.5,
            "ytick.minor.width": 0.3,
            "font.family": "monospace",
            "font.stretch": "semi-expanded",
            # The default edge colors for scatter plots.
            "scatter.edgecolors": "black",
            "mathtext.bf": "monospace:bold",
            "mathtext.cal": "monospace:bold",
            "mathtext.it": "monospace:italic",
            "mathtext.rm": "monospace",
            "mathtext.sf": "monospace",
            "mathtext.tt": "monospace",
            "mathtext.fallback": "cm",
            "mathtext.default": 'it'
        })

        if usetex is True:
            rcParams.update({
                "text.latex.preamble": '\n'.join([
                    '\\usepackage{amsmath}'
                    '\\usepackage[T1]{fontenc}',
                    '\\usepackage{courier}',
                    '\\usepackage[variablett]{lmodern}',
                    '\\usepackage[LGRgreek]{mathastext}',
                    '\\renewcommand{\\rmdefault}{\\ttdefault}'
                ])
            })

    if style == 'nature':
        rcParams.update({
            "font.family": "sans-serif",
            # The default edge colors for scatter plots.
            "scatter.edgecolors": "black",
            "mathtext.fontset": "stixsans"
        })


def calc_observables(params, name='NMF', dt=0.1):
    if name == 'NMF_ZH':
        from popsed.models import NMF_ZH
        model = NMF_ZH(burst=True, peraa=True)
    else:
        from popsed.models import NMF
        model = NMF(burst=True, peraa=True)

    sfr = np.zeros(len(params))
    age = np.zeros(len(params))
    logmsurv = np.zeros(len(params))

    # Calculate SFR and age and survived mass
    for i in range(len(params)):
        tt = params[i]
        sfr[i] = model.avgSFR(tt[:-1], zred=float(tt[-1]), dt=dt)
        age[i] = model.tage_MW(tt[:-1], zred=float(tt[-1]))
        logmsurv[i] = model.survMass(tt[:-1], zred=float(tt[-1]))
    
    # Calculate Z_MW
    if name == 'NMF_ZH':
        zmw = np.zeros(len(params))
        for i in range(len(params)):
            tt = params[i]
            zmw[i] = model.Z_MW(tt[:-1], zred=float(tt[-1]))

    if name == 'NMF_ZH':
        logzsol = np.log10(zmw / 0.019)
    else:
        logzsol = params[:, 7]
        
    redshift = params[:, -1]
    logmstar = params[:, 0]

    obs_dict = {'logmstar': logmstar, 'logmsurv': logmsurv, 'logzsol': logzsol,
                'sfr': sfr, 'age': age, 'redshift': redshift,}
    
    return obs_dict


############### Survived mass from DSPS ##############
# https://github.com/ArgonneCPAC/dsps/blob/main/dsps/imf/surviving_mstar.py#L31
######################################################
from collections import OrderedDict
from copy import deepcopy

PARAMS = OrderedDict(x0=2, yhi=0.58)
DEFAULT_PARAMS = OrderedDict(
    a=0.225,
    b=8.0,
    lgk1=-0.5,
    d=0.145,
    e=0.08,
    f=6.5,
    lgk2=0.7,
    h=0.005,
)
SALPETER_PARAMS = deepcopy(DEFAULT_PARAMS)
CHABRIER_PARAMS = deepcopy(DEFAULT_PARAMS)
KROUPA_PARAMS = deepcopy(DEFAULT_PARAMS)
VAN_DOKKUM_PARAMS = deepcopy(DEFAULT_PARAMS)

SALPETER_PARAMS.update(a=0.13, d=0.055)
CHABRIER_PARAMS.update(a=0.225, d=0.145)
KROUPA_PARAMS.update(a=0.211, d=0.13)
VAN_DOKKUM_PARAMS.update(a=0.238, d=0.156)

def logistic(x):
    return 1 / (1 + np.exp(-x))
def _sigmoid(x, x0, k, ylo, yhi):
    height_diff = yhi - ylo
    return ylo + height_diff * logistic(k * (x - x0))
def _sig_slope(x, xtp, ytp, x0, slope_k, lo, hi):
    slope = _sigmoid(x, x0, slope_k, lo, hi)
    return ytp + slope * (x - xtp)

def _returned_mass(lg_age_yr, a, b, lgk1, d, e, f, lgk2, h):
    k1 = 10**lgk1
    xtp = b
    z = _sig_slope(lg_age_yr, xtp, a, b, k1, d, e)

    k2 = 10**lgk2
    h = _sigmoid(lg_age_yr, f, k2, h, z)
    return h

def surviving_mstar(
    lg_age_yr,
    a=DEFAULT_PARAMS["a"],
    b=DEFAULT_PARAMS["b"],
    lgk1=DEFAULT_PARAMS["lgk1"],
    d=DEFAULT_PARAMS["d"],
    e=DEFAULT_PARAMS["e"],
    f=DEFAULT_PARAMS["f"],
    lgk2=DEFAULT_PARAMS["lgk2"],
    h=DEFAULT_PARAMS["h"],
):
    """Calculate the fraction of stellar mass that survives as a population ages.

    Default behavior assumes Chabrier IMF.
    Calibrations for the following alternative parameters are also available:
        SALPETER_PARAMS, KROUPA_PARAMS, VAN_DOKKUM_PARAMS

    Parameters
    ----------
    lg_age_yr : ndarray of shape (n, )
        Base-10 log of the age of the stellar population in years

    Returns
    -------
    frac_surviving : ndarray of shape (n, )
        Surviving fraction

    """
    frac_returned = _returned_mass(lg_age_yr, a, b, lgk1, d, e, f, lgk2, h)
    return 1 - frac_returned