'''
Module for SPS models. Taken from https://github.com/changhoonhahn/provabgs with modifications.
'''
import os
import h5py
import pickle
import warnings
import numpy as np
import scipy.interpolate as Interp
# --- astropy ---
from astropy import units as U
from astropy.cosmology import Planck15
# --- gqp_mc ---
from . import utils

try:
    import fsps
except ImportError:
    warnings.warn('import error with fsps; only use emulators')
warnings.filterwarnings("ignore", category=RuntimeWarning)

# Some constants
lsun = 3.846e33  # erg/s
pc = 3.085677581467192e18  # in cm
lightspeed = 2.998e18  # AA/s
ckms = 2.998e5  # km/s
jansky_mks = 1e-26
jansky_cgs = 1e-23
# value to go from L_sun/AA to erg/s/cm^2/AA at 10pc
to_cgs_at_10pc = lsun / (4.0 * np.pi * (pc * 10)**2)


class Model(object):
    '''
    Base class object for different SPS models. Different `Model` objects
    specify different SPS model. The primary purpose of the `Model` class is to
    evaluate the SED given a set of parameter values. 
    '''

    def __init__(self, cosmo=None, **kwargs):
        if cosmo is None:
            self.cosmo = Planck15  # cosmology

        self.age_univ = self.cosmo.age(0).to(
            'Gyr').value  # age of universe [Gyr]

        self._init_model(**kwargs)

        # interpolators for speeding up cosmological calculations
        _z = np.linspace(0.0, 5.0, 1000)
        _tage = self.cosmo.age(_z).value
        _d_lum_cm = self.cosmo.luminosity_distance(_z).to(
            U.cm).value  # luminosity distance in cm

        self._tage_z_interp = \
            Interp.InterpolatedUnivariateSpline(_z, _tage, k=3)
        self._z_tage_interp = \
            Interp.InterpolatedUnivariateSpline(_tage[::-1], _z[::-1], k=3)
        self._d_lum_z_interp = \
            Interp.InterpolatedUnivariateSpline(_z, _d_lum_cm, k=3)
        print('Initialize NMF-based SPS model')
        print('Input parameters : %s' % ', '.join(self._parameters))

    def sed(self, tt, zred, vdisp=0., wavelength=None, resolution=None, filters=None):
        '''
        Compute the redshifted spectral energy distribution (SED) for 
        a given set of parameter values and redshift.


        Parameters
        ----------
        tt : 2-d array
            [Nsample,Nparam] SPS parameters     

        zred : float or array_like
            redshift of the SED 

        vdisp : float or array_like
            velocity dispersion  

        wavelength : array_like [Nwave,]
            If you want to use your own wavelength. If specified, the model
            will interpolate the spectra to the specified wavelengths. By
            default, it will use the speculator wavelength
            (Default: None)  

        resolution : array_like [N,Nwave]
            resolution matrix (e.g. DESI data provides a resolution matrix)  

        filters : object
            Photometric bandpass filter to generate photometry.
            `speclite.FilterResponse` object. 


        Returns
        -------
        outwave : [Nsample, Nwave]
            output wavelengths in angstrom. 

        outspec : [Nsample, Nwave]
            the redshifted SED in units of 1e-17 * erg/s/cm^2/Angstrom.
        '''
        tt = np.atleast_2d(tt)
        zred = np.atleast_1d(zred)
        vdisp = np.atleast_1d(vdisp)
        ntheta = tt.shape[1]

        assert tt.shape[0] == zred.shape[0]

        outwave, outspec, maggies = [], [], []
        for _tt, _zred in zip(tt, zred):
            #_tage = self.cosmo.age(_zred).value
            _tage = self._tage_z_interp(_zred)

            # get SSP luminosity
            wave_rest, lum_ssp = self._sps_model(_tt, _tage)

            # redshift the spectra
            w_z = wave_rest * (1. + _zred)
            d_lum = self._d_lum_z_interp(_zred)
            # flux_z = lum_ssp * utils.Lsun() / (4. * np.pi * d_lum**2) / (1. + _zred) * 1e17 # 10^-17 ergs/s/cm^2/Ang
            flux_z = lum_ssp * 3.846e33 / \
                (4. * np.pi * d_lum**2) / (1. + _zred)  # 10^-17 ergs/s/cm^2/Ang

            # apply velocity dispersion
            if vdisp == 0:
                wave_smooth = w_z
                flux_smooth = flux_z
            else:
                wave_smooth, flux_smooth = self._apply_vdisp(
                    w_z, flux_z, vdisp)

            if wavelength is None:
                outwave.append(wave_smooth)
                outspec.append(flux_smooth)
            else:
                outwave.append(wavelength)

                # resample flux to input wavelength
                if np.all(np.diff(wavelength) >= 0):
                    resampflux = utils.trapz_rebin(
                        wave_smooth, flux_smooth, xnew=wavelength)
                else:
                    isort = np.argsort(wavelength)
                    resampflux = np.zeros(len(wavelength))
                    resampflux[isort] = utils.trapz_rebin(wave_smooth, flux_smooth,
                                                          xnew=wavelength[isort])

                if resolution is not None:
                    # apply resolution matrix
                    _i = 0
                    for res in np.atleast_1d(resolution):
                        _res = utils.Resolution(res)
                        resampflux[_i:_i + res.shape[-1]
                                   ] = _res.dot(resampflux[_i:_i + res.shape[-1]])
                        _i += res.shape[-1]
                outspec.append(resampflux)

            if filters is not None:
                # calculate photometry from SEDs
                flux_z, w_z = filters.pad_spectrum(np.atleast_2d(flux_z) *
                                                   1e-17 * U.erg / U.s / U.cm**2 / U.Angstrom,
                                                   w_z * U.Angstrom)
                _maggies = filters.get_ab_maggies(flux_z, wavelength=w_z)
                maggies.append(np.array(list(_maggies[0])) * 1e9)

        if len(outwave) == 1:
            outwave = outwave[0]
            outspec = outspec[0]
            if filters is not None:
                maggies = maggies[0]
        else:
            outwave = np.array(outwave)
            outspec = np.array(outspec)
            if filters is not None:
                maggies = np.array(maggies)

        from matplotlib import pyplot as plt
        plt.plot(w_z, flux_z)
        # plt.plot(outwave, outspec)
        plt.xlim(9900, 15200)

        if filters is None:
            return outwave, outspec
        else:
            return outwave, outspec, maggies

    def _init_model(self, **kwargs):
        return None

    def _apply_vdisp(self, wave, flux, vdisp):
        ''' apply velocity dispersion by first rebinning to log-scale
        wavelength then convolving vdisp. 

        Notes
        -----
        * code lift from https://github.com/desihub/desigal/blob/d67a4350bc38ae42cf18b2db741daa1a32511f8d/py/desigal/nyxgalaxy.py#L773
        * confirmed that it reproduces the velocity dispersion calculations in
        prospector
        (https://github.com/bd-j/prospector/blob/41cbdb7e6a13572baea59b75c6c10100e7c7e212/prospect/utils/smoothing.py#L17)
        '''
        if vdisp <= 0:
            return wave, flux
        from scipy.ndimage import gaussian_filter1d
        pixkms = 10.0                                 # SSP pixel size [km/s]
        dlogwave = pixkms / 2.998e5 / np.log(10)
        wlog = 10**np.arange(np.log10(wave.min() + 10.),
                             np.log10(wave.max() - 10.), dlogwave)
        flux_wlog = utils.trapz_rebin(wave, flux, xnew=wlog, edges=None)
        # convolve
        sigma = vdisp / pixkms  # in pixels
        smoothflux = gaussian_filter1d(flux_wlog, sigma=sigma, axis=0)
        return wlog, smoothflux

    def _parse_theta(self, tt):
        ''' parse given array of parameter values 
        '''
        tt = np.atleast_2d(tt.copy())

        assert tt.shape[1] == len(self._parameters), 'given theta has %i instead of %i dims' % (
            tt.shape[1], len(self._parameters))

        theta = {}
        for i, param in enumerate(self._parameters):
            theta[param] = tt[:, i]
        return theta


class NMF(Model):
    '''
    Modified from provabgs.models.NMF. Replace metallicity history with
    a single metallicity.

    SPS model with non-parametric star formation history and flexible dust 
    attenuation model. The SFH is based on non-negative
    matrix factorization (NMF) bases (Tojeiro+in prep). The dust attenuation
    uses a standard Charlot & Fall dust model (in the language of FSPS, `dust_type=4`). 
    The IMF is Chabrier IMF.

    The SFH uses 4 NMF bases. If you specify `burst=True`, the SFH will
    include an additional burst component. 

    The metallicity here is absolute metallicity. Recall that Z_sun = 0.0190. 

    The dust attenuation is modeled using a 3 parameter Charlot & Fall model.
    `dust1` is tau_BC, the optical depth of dust attenuation of birth cloud
    that only affects young stellar population. `dust2` is tau_ISM, the optical
    depth of dust attenuation from the ISM, which affects all stellar emission.
    `dust_index` is the attenuation curve slope offset from Calzetti.

    If you specify `emulator=True`, the model will use a PCA NN emulator to
    evaluate the SPS model, rather than Flexible Stellar Population Synthesis.
    The emulator has <1% level accuracy and its *much* faster than FSPS. I
    recommend using the emulator for parameter exploration. 


    Parameters
    ----------
    burst : bool
        If True include a star bursts component in SFH. (default: True) 

    emulator : bool
        If True, use emulator rather than running FSPS. 

    cosmo : astropy.comsology object
        specify cosmology. If cosmo=None, NMF uses astorpy.cosmology.Planck15.

    peraa : bool
        IF True, the output spectrum will be in unit of L_sun/AA (per Angstrom).
        Otherwise, the output will be in unit of L_sun/Hz. (default: False).
        To convert from L_sun/Hz to L_sun/AA, multiply L_sun/Hz by lightspeed / wave**2.
        To convert L_sun/Hz to erg/s/cm^2/AA at 10 pc, multiply by `to_cgs_at_10pc`.

    Notes 
    -----
    * only supports 4 component SFH with or without burst and 2 component ZH 
    * only supports Calzetti+(2000) attenuation curve and Chabrier IMF. 
    '''

    def __init__(self, burst=True, emulator=False, cosmo=None, peraa=False):
        self._ssp = None
        self._burst = burst
        self._emulator = emulator
        self._peraa = peraa
        # metallicity range set by MIST isochrone
        self._Z_min = 4.49043431e-05
        self._Z_max = 4.49043431e-02
        super().__init__(cosmo=cosmo)  # initializes the model

    def _fsps(self, tt, tage):
        '''
        FSPS SED model. If `emulator=False`, FSPS is used to evaluate the
        SED rather than the emulator. First, SFH and ZH are constructed from
        the `tt` parameters. Then stellar population synthesis is used to get
        the spectra of each time bin. Afterwards, they're combined to construct
        the SED. 

        Parameters 
        ----------
        tt : 1d array 
            Nparam array that specifies the parameter values 

        tage : float 
            age of galaxy 


        Returns
        -------
        wave_rest : array_like[Nwave] 
            rest-frame wavelength of SSP flux 


        lum_ssp : array_like[Nwave] 
            FSPS SSP luminosity in units of Lsun/A

        Notes
        -----
        * 12/23/2020: age of SSPs are no longer set to the center of the
          lookback time as this  ignores the youngest tage ~ 0 SSP, which have
          significant contributions. Also we set a minimum tage = 1e-8 because
          FSPS returns a grid for tage = 0 but 1e-8 gets the youngest isochrone
          anyway. 
        * 2021/06/24: log-spaced lookback time implemented
        '''

        if self._ssp is None:
            self._ssp_initiate()  # initialize FSPS StellarPopulation object
        theta = self._parse_theta(tt)

        assert np.isclose(np.sum([theta['beta1_sfh'], theta['beta2_sfh'],
                                  theta['beta3_sfh'], theta['beta4_sfh']],
                                 axis=0), 1.).all(), "SFH basis coefficients should add up to 1"

        # NMF SFH(t) noramlized to 1 **without burst**
        tlb_edges, sfh = self.SFH(np.hstack(
            [np.zeros(1), tt[1:]]), tage=tage,
            _burst=False)
        # Replace whatever stellar mass with 1 Msun
        # 0.0 stands for logmstar = 0.0, i.e., normalized to 1 M_sun

        '''
        # NMF ZH at lookback time bins
        _, zh = self.ZH(tt, tage=tage)
        # I don't use ZH. I use log_Z directly.
        '''
        tages = 0.5 * (tlb_edges[1:] + tlb_edges[:-1])  # ages of SSP
        dt = np.diff(tlb_edges)  # bin widths

        # look over log-spaced lookback time bins and add up SSPs
        for i, tage in enumerate(tages):
            m = 1e9 * dt[i] * sfh[i]  # mass formed in this bin
            if m == 0 and i != 0:
                continue

            self._ssp.params['logzsol'] = theta['logzsol']  # log(Z/Zsun)
            self._ssp.params['dust1'] = theta['dust1']
            self._ssp.params['dust2'] = theta['dust2']
            self._ssp.params['dust_index'] = theta['dust_index']

            wave_rest, lum_i = self._ssp.get_spectrum(
                tage=tage, peraa=self._peraa)
            # If peraa=True, the spectrum will be in units of Lsun/AA
            # note that this spectrum is normalized such that the total formed
            # mass = 1 Msun

            if i == 0:
                lum_ssp = np.zeros(len(wave_rest))
            lum_ssp += m * lum_i

        # add burst contribution
        if self._burst:
            fburst = theta['fburst']
            tburst = theta['tburst']

            lum_burst = np.zeros(lum_ssp.shape)
            # if starburst is within the age of the galaxy
            if tburst < tage:
                _, lum_burst = self._fsps_burst(tt)

            # renormalize NMF contribution
            lum_ssp *= (1. - fburst)

            # add in burst contribution
            lum_ssp += fburst * lum_burst

        # normalize by stellar mass
        lum_ssp *= (10**theta['logmstar'])

        return wave_rest, lum_ssp

    def _fsps_burst(self, tt, debug=False):
        '''
        Dust attenuated spectra of single stellar population that
        corresponds to the single star-burst event. The spectrum is normalized 
        such that the total formed mass is 1 Msun, **not** fburst. Therefore 
        when adding this burst component back, `fburst` should be multiplied. 
        The spectrum is calculated using FSPS. 
        '''
        if self._ssp is None:
            self._ssp_initiate()  # initialize FSPS StellarPopulation object
        theta = self._parse_theta(tt)

        tburst = theta['tburst']
        assert tburst > 1e-2, "burst currently only supported for tburst > 1e-2 Gyr = 10 Myr"

        # get metallicity at tburst
        '''
        tt_zh = np.array([theta['gamma1_zh'], theta['gamma2_zh']])
        zburst = np.sum(np.array([tt_zh[i] * self._zh_basis[i](tburst)
                                  for i in range(self._N_nmf_zh)])).clip(self._Z_min, self._Z_max)
        if debug:
            print('zburst=%e' % zburst)
            print('dust2=%f' % dust2)
            print('dust_index=%f' % dust_index)
        '''
        # luminosity of SSP at tburst
        # np.log10(zburst/0.0190)  # log(Z/Zsun)
        self._ssp.params['logzsol'] = theta['logzsol']
        self._ssp.params['dust1'] = 0.  # no birth cloud attenuation for tage > 1e-2 Gyr
        self._ssp.params['dust2'] = theta['dust2']
        self._ssp.params['dust_index'] = theta['dust_index']

        wave_rest, lum_burst = self._ssp.get_spectrum(
            tage=tburst, peraa=self._peraa)
        # If peraa=True, the spectrum will be in units of Lsun/AA
        # note that this spectrum is normalized such that the total formed
        # mass = 1 Msun
        return wave_rest, lum_burst

    def SFH(self, tt, zred=None, tage=None, _burst=True):
        ''' star formation history for given set of parameter values and
        redshift.

        Parameters
        ----------
        tt : 1d or 2d array 
            N-param or NxN param array of parameter values 

        tage : float
            age of the galaxy 

        Returns
        -------
        tedges: 1d array 
            bin edges of log-spaced look back time

        sfh : 1d or 2d array 
            star formation history in Msun/yr
        '''
        if zred is None and tage is None:
            raise ValueError(
                "specify either the redshift or age of the galaxy")
        if tage is None:
            assert isinstance(zred, float)
            tage = self.cosmo.age(zred).value  # age in Gyr

        theta = self._parse_theta(tt)

        # sfh nmf basis coefficients
        tt_sfh = np.array([theta['beta1_sfh'], theta['beta2_sfh'],
                           theta['beta3_sfh'], theta['beta4_sfh']]).T

        # log-spaced lookback time bin edges
        tlb_edges = utils.tlookback_bin_edges(tage)

        sfh_basis_tlb = np.array([
            utils.trapz_rebin(self._t_lb_hr, _sfh_basis, edges=tlb_edges)
            for _sfh_basis in self._sfh_basis_hr])

        sfh = np.sum(np.array([tt_sfh[:, i][:, None] *
                               sfh_basis_tlb[i][None, :] for i in range(self._N_nmf_sfh)]),
                     axis=0)

        sfh /= np.sum(1e9 * np.diff(tlb_edges) * sfh,
                      axis=1)[:, None]  # normalize

        # add starburst
        if self._burst and _burst:
            # fraction of stellar mass from star burst
            fburst = theta['fburst']
            tburst = theta['tburst']  # time of star burst

            noburst = (tburst > tage)
            fburst[noburst] = 0.

            # add normalized starburst to SFH
            sfh *= (1. - fburst)[:, None]
            sfh += fburst[:, None] * self._single_SFH_burst(tburst, tlb_edges)

        # multiply by stellar mass
        sfh *= 10**theta['logmstar'][:, None]

        if np.atleast_2d(tt).shape[0] == 1:
            return tlb_edges, sfh[0]
        return tlb_edges, sfh

    def _single_SFH_burst(self, tburst, tedges):
        ''' place a single star-burst event on the SFH
        '''
        tburst = np.atleast_1d(tburst)
        dts = np.diff(tedges)

        # burst within the age of the galaxy
        has_burst = (tburst < tedges.max())

        # log-spaced lookback time bin with burst
        iburst = np.digitize(tburst[has_burst], tedges) - 1

        sfh = np.zeros((len(tburst), len(tedges) - 1))
        sfh[has_burst, iburst] += 1. / (1e9 * dts[iburst])
        return sfh

    def avgSFR(self, tt, zred=None, tage=None, dt=1):
        ''' given a set of parameter values `tt` and redshift `zred`, calculate
        SFR averaged over `dt` Gyr. 

        parameters
        ----------
        tt : array_like[Ntheta, Nparam]
           Parameter values of [log M*, b1SFH, b2SFH, b3SFH, b4SFH, g1ZH, g2ZH,
           'dust1', 'dust2', 'dust_index']. 

        zred : float 
            redshift

        tage : float 
            age of galaxy 

        dt : float
            Gyrs to average the SFHs 
        '''
        if zred is None and tage is None:
            raise ValueError('specify either zred or tage')
        if zred is not None and tage is not None:
            raise ValueError('specify either zred or tage')
        if tage is None:
            assert isinstance(zred, float)
            tage = self.cosmo.age(zred).value  # age in Gyr

        theta = self._parse_theta(tt)

        # sfh nmf basis coefficients
        tt_sfh = np.array([theta['beta1_sfh'], theta['beta2_sfh'],
                           theta['beta3_sfh'], theta['beta4_sfh']]).T

        # log-spaced lookback time bin edges
        tlb_edges = utils.tlookback_bin_edges(tage)
        assert dt < tlb_edges[-1]

        sfh_basis_tlb = np.array([
            utils.trapz_rebin(self._t_lb_hr, _sfh_basis, edges=tlb_edges)
            for _sfh_basis in self._sfh_basis_hr])

        sfh = np.sum(np.array([tt_sfh[:, i][:, None] *
                               sfh_basis_tlb[i][None, :] for i in range(self._N_nmf_sfh)]),
                     axis=0)

        sfh /= np.sum(np.diff(tlb_edges) * sfh, axis=1)[:, None]  # normalize

        # calculate stellar mass formed over 0-dt
        i_dt = np.digitize(dt, tlb_edges) - 1
        Mform = np.sum(np.diff(tlb_edges)[
                       :i_dt][None, :] * sfh[:, :i_dt], axis=1)
        Mform += (dt - tlb_edges[i_dt]) * sfh[:, i_dt]

        # add starburst event
        if self._burst:
            # fraction of stellar mass from star burst
            fburst = theta['fburst']
            tburst = theta['tburst']  # time of star burst

            noburst = (tburst > tage)
            fburst[noburst] = 0.
            Mform *= (1. - fburst)

            burst_dt = (tburst < dt)
            Mform[burst_dt] += theta['fburst'][burst_dt]

        # multiply by stellar mass
        avg_sfr = Mform * 10**theta['logmstar'] / dt / 1e9
        return avg_sfr

    def ZH(self, tt, zred=None, tage=None):
        ''' metallicity history for given set of parameters. metallicity is
        parameterized using a 2 component NMF basis. The parameter values
        specify the coefficient of the components and this method returns the
        linear combination of the two. 

        parameters
        ----------
        tt : array_like[N,Nparam]
           parameter values of the model 

        zred : float
            redshift of galaxy/csp

        Returns 
        -------
        tedge : 1d array
            bin edges of lookback time

        zh : 2d array
            metallicity at cosmic time t --- ZH(t) 
        '''
        if zred is None and tage is None:
            raise ValueError(
                "specify either the redshift or age of the galaxy")
        if tage is None:
            assert isinstance(zred, float)
            tage = self.cosmo.age(zred).value  # age in Gyr
        theta = self._parse_theta(tt)

        # metallicity history basis coefficients
        tt_zh = np.array([theta['gamma1_zh'], theta['gamma2_zh']]).T

        # log-spaced lookback time bin edges
        tlb_edges = utils.tlookback_bin_edges(tage)
        tlb = 0.5 * (tlb_edges[1:] + tlb_edges[:-1])

        # metallicity basis
        _z_basis = np.array([self._zh_basis[i](tlb)
                            for i in range(self._N_nmf_zh)])

        # get metallicity history
        zh = np.sum(np.array([tt_zh[:, i][:, None] * _z_basis[i][None, :] for i in
                              range(self._N_nmf_zh)]), axis=0).clip(self._Z_min, self._Z_max)

        if tt_zh.shape[0] == 1:
            return tlb_edges, zh[0]
        return tlb_edges, zh

    def Z_MW(self, tt, tage=None, zred=None):
        ''' given theta calculate mass weighted metallicity using the ZH NMF
        bases. 
        '''
        if tage is None and zred is None:
            raise ValueError("specify either zred or tage")
        if tage is not None and zred is not None:
            raise ValueError("specify either zred or tage")

        theta = self._parse_theta(tt)
        tlb_edge, sfh = self.SFH(tt, tage=tage, zred=zred)  # get SFH
        _, zh = self.ZH(tt, tage=tage, zred=zred)

        # mass weighted average
        z_mw = np.sum(1e9 * np.diff(tlb_edge)
                      [None, :] * sfh * zh, axis=1) / (10**theta['logmstar'])
        return z_mw

    def tage_MW(self, tt, tage=None, zred=None):
        ''' given theta calculate mass weighted age of the galaxy 
        '''
        if tage is None and zred is None:
            raise ValueError("specify either zred or tage")
        if tage is not None and zred is not None:
            raise ValueError("specify either zred or tage")

        theta = self._parse_theta(tt)
        tlb_edge, sfh = self.SFH(tt, tage=tage, zred=zred)  # get SFH
        th = 0.5 * (tlb_edge[1:] + tlb_edge[:-1])

        # mass weighted average
        t_mw = np.sum(1e9 * np.diff(tlb_edge)
                      [None, :] * sfh * th, axis=1) / (10**theta['logmstar'])
        return t_mw

    def survMass(self, tt, zred=None, tage=None):
        ''' given a set of parameter values `tt` and redshift `zred`, 
        calculate the surviving stellar mass. 

        parameters
        ----------
        tt : array_like[Ntheta, Nparam]
        Parameter values of [log M*, b1SFH, b2SFH, b3SFH, b4SFH, g1ZH, g2ZH,
        'dust1', 'dust2', 'dust_index']. 

        zred : float 
            redshift

        tage : float 
            age of galaxy, in Gyr

        '''
        from .utils import surviving_mstar
        if zred is None and tage is None:
            raise ValueError('specify either zred or tage')
        if zred is not None and tage is not None:
            raise ValueError('specify either zred or tage')
        if tage is None:
            assert isinstance(zred, float)
            tage = self.cosmo.age(zred).value  # age in Gyr
        
        theta = self._parse_theta(tt)
        # Get SFH
        tlb_edges, sfh = self.SFH(tt, zred=zred, _burst=self._burst) # Mstar / yr
        tages = 0.5 * (tlb_edges[1:] + tlb_edges[:-1])  # ages of SSP
        dt = np.diff(tlb_edges)  # bin widths

        Msurv = 0 # surviving stellar mass
        # look over log-spaced lookback time bins and add up SSPs
        for i, tage in enumerate(tages):
            m = 1e9 * dt[i] * sfh[i]  # mass formed in this bin
            if m == 0 and i != 0:
                continue
            Msurv += m * surviving_mstar(np.log10(tage * 1e9))
            
        return np.log10(Msurv)
        
        
    def _load_NMF_bases(self, name='tojeiro.4comp'):
        ''' read in NMF SFH and ZH bases. These bases are used to reduce the
        dimensionality of the SFH and ZH. 
        '''
        dir_dat = os.path.join(os.path.dirname(
            os.path.realpath(__file__)), 'dat')
        if name == 'tojeiro.4comp':
            fsfh = os.path.join(
                dir_dat, 'NMF_2basis_SFH_components_nowgt_lin_Nc4.txt')
            # fzh = os.path.join(
            #     dir_dat, 'NMF_2basis_Z_components_nowgt_lin_Nc2.txt')
            ft = os.path.join(dir_dat, 'sfh_t_int.txt')

            # basis order is jumbled up it should be [2 ,0, 1, 3]
            nmf_sfh = np.loadtxt(fsfh)[:, ::-1]
            # nmf_zh = np.loadtxt(fzh)[:, ::-1]
            nmf_t = np.loadtxt(ft)[::-1]  # look back time

            self._nmf_t_lb_sfh = nmf_t
            # self._nmf_t_lb_zh = nmf_t
            self._nmf_sfh_basis = np.array(
                [nmf_sfh[2], nmf_sfh[0], nmf_sfh[1], nmf_sfh[3]])
            # self._nmf_zh_basis = nmf_zh
        elif name in ['tng.4comp', 'tng.6comp']:
            raise NotImplementedError('TNG is not implemented yet')
            # icomp = int(name.split('.')[-1][0])
            # fsfh = os.path.join(dir_dat, 'NMF_basis.sfh.tng%icomp.txt' % icomp)
            # fzh = os.path.join(
            #     dir_dat, 'NMF_2basis_Z_components_nowgt_lin_Nc2.txt')
            # ftsfh = os.path.join(dir_dat, 't_sfh.tng%icomp.txt' % icomp)
            # ftzh = os.path.join(dir_dat, 'sfh_t_int.txt')

            # nmf_sfh = np.loadtxt(fsfh, unpack=True)
            # nmf_zh = np.loadtxt(fzh)
            # nmf_tsfh = np.loadtxt(ftsfh)  # look back time
            # nmf_tzh = np.loadtxt(ftzh)  # look back time

            # self._nmf_t_lb_sfh = nmf_tsfh
            # self._nmf_t_lb_zh = nmf_tzh[::-1]
            # self._nmf_sfh_basis = nmf_sfh
            # self._nmf_zh_basis = nmf_zh[:, ::-1]
        else:
            raise NotImplementedError

        self._Ncomp_sfh = self._nmf_sfh_basis.shape[0]
        # self._Ncomp_zh = self._nmf_zh_basis.shape[0]

        # SFH bases as a function of lookback time
        self._sfh_basis = [
            Interp.InterpolatedUnivariateSpline(
                self._nmf_t_lb_sfh,
                self._nmf_sfh_basis[i], k=1)
            for i in range(self._Ncomp_sfh)
        ]
        # self._zh_basis = [
        #     Interp.InterpolatedUnivariateSpline(
        #         self._nmf_t_lb_zh,
        #         self._nmf_zh_basis[i], k=1)
        #     for i in range(self._Ncomp_zh)]

        # high resolution tabulated SFHs used in the SFH calculation
        self._t_lb_hr = np.linspace(0., self.age_univ, int(5e4))
        self._sfh_basis_hr = [sfh_basis(self._t_lb_hr)
                              for sfh_basis in self._sfh_basis]
        return None

    def _init_model(self, **kwargs):
        ''' some under the hood initalization of the model and its
        parameterization. 
        '''
        # load 4 component NMF bases from Rita
        self._load_NMF_bases(name='tojeiro.4comp')
        self._N_nmf_sfh = 4  # 4 NMF component SFH

        if not self._burst:
            self._parameters = [
                'logmstar',
                'beta1_sfh',
                'beta2_sfh',
                'beta3_sfh',
                'beta4_sfh',
                'logzsol',
                'dust1',
                'dust2',
                'dust_index']
        else:
            self._parameters = [
                'logmstar',
                'beta1_sfh',
                'beta2_sfh',
                'beta3_sfh',
                'beta4_sfh',
                'fburst',
                # lookback time of the universe when burst occurs (tburst < tage)
                'tburst',
                'logzsol',
                'dust1',
                'dust2',
                'dust_index']

        if not self._emulator:
            self._sps_model = self._fsps
        else:
            self._sps_model = self._emu
            self._load_emulator()

        return None

    def _ssp_initiate(self):
        ''' initialize sps (FSPS StellarPopulaiton object) 
        '''
        sfh = 0  # tabulated SFH
        dust_type = 4  # dust1, dust2, and dust_index
        imf_type = 1  # chabrier

        self._ssp = fsps.StellarPopulation(
            zcontinuous=1,          # interpolate metallicities
            sfh=sfh,                # sfh type
            dust_type=dust_type,    # dust model type
            imf_type=imf_type)      # chabrier
        return None

    def _emu(self, tt, tage):
        ''' PCA neural network emulator of FSPS SED model. If `emulator=True`,
        this emulator is used instead of `_fsps`. The emulator is *much* faster
        than the FSPS.

        Parameters
        ----------
        tt : 1d array
            Nparam array that specifies the parameter values

        tage : float
            age of galaxy

        Returns
        -------
        wave_rest : array_like[Nwave]
            rest-frame wavelength of SSP flux

        lum_ssp : array_like[Nwave]
            FSPS SSP luminosity in units of Lsun/A

        Notes
        -----
        * June 11, 2021: burst component no longer uses an emulator because
            it's fast enough.
        '''

        theta = self._parse_theta(tt)

        assert np.isclose(np.sum([theta['beta1_sfh'], theta['beta2_sfh'],
                                  theta['beta3_sfh'], theta['beta4_sfh']]), 1.), "SFH basis coefficients should add up to 1"

        # get redshift with interpolation
        #zred = self._z_tage_interp(tage)

        tt_nmf = np.concatenate([theta['beta1_sfh'], theta['beta2_sfh'],
                                 theta['beta3_sfh'], theta['beta4_sfh'], theta['gamma1_zh'],
                                 theta['gamma2_zh'], theta['dust1'], theta['dust2'],
                                 theta['dust_index'], [tage]])  # zred]])

        assert theta['gamma2_zh'] < 2.0e-2
        assert theta['gamma1_zh'] > 4.5e-5
        # NMF from emulator
        lum_ssp = np.exp(self._emu_nmf(tt_nmf))

        # add burst contribution
        if self._burst:
            fburst = theta['fburst']
            tburst = theta['tburst']

            lum_burst = np.zeros(lum_ssp.shape)
            # if starburst is within the age of the galaxy
            if tburst < tage and fburst > 0.:
                lum_burst = np.exp(self._emu_burst(tt))
                #_w, _lum_burst = self._fsps_burst(tt)
                #lum_burst = _lum_burst[(_w > 2300.) & (_w < 60000.)]

            # renormalize NMF contribution
            lum_ssp *= (1. - fburst)

            # add in burst contribution
            lum_ssp += fburst * lum_burst

        # normalize by stellar mass
        lum_ssp *= (10**theta['logmstar'])

        return self._nmf_emu_waves, lum_ssp

    def _emu_nmf(self, tt):
        ''' emulator for the SED from the NMF star formation history.

        Parameters
        ----------
        tt : 1d array 
            Nparam array that specifies 
            [beta1_sfh, beta2_sfh, beta3_sfh, beta4_sfh, gamma1_zh, gamma2_zh, dust1, dust2, dust_index, redshift] 

        Returns
        -------
        logflux : array_like[Nwave,] 
            (natural) log of (SSP luminosity in units of Lsun/A)
        '''
        # untransform SFH coefficients from Dirichlet distribution
        _tt = np.empty(9)
        _tt[0] = (1. - tt[0]).clip(1e-8, None)
        for i in range(1, 3):
            _tt[i] = 1. - (tt[i] / np.prod(_tt[:i]))
        _tt[3:] = tt[4:]
        _tt = _tt.astype(np.float32)

        logflux = []
        for iwave in range(self._nmf_n_emu):  # wave bins

            W0, W1, W2, b0, b1, b2, alphas_, betas_, param_shift, param_scale,\
                pca_shift_, pca_scale_, spectrum_shift_, spectrum_scale_,\
                pca_transform_matrix_ = self._nmf_emu_params[iwave]
            n_layers = self._nmf_emu_nlayers[iwave]

            # PCA coefficients
            pca_emu = utils.pcaMLP(_tt, W0, W1, W2, b0, b1, b2, alphas_, betas_,
                                   param_shift, param_scale, pca_shift_, pca_scale_,
                                   pca_transform_matrix_, n_layers)
            logflux.append(pca_emu * spectrum_scale_ + spectrum_shift_)

        return np.concatenate(logflux)

    def _emu_burst(self, tt, debug=False):
        ''' calculate the dust attenuated luminosity contribution from a SSP
        that corresponds to the burst using an emulator. This spectrum is
        normalized such that the total formed mass is 1 Msun, **not** fburst 

        Notes
        -----
        * currently luminosity contribution is set to 0 if tburst > 13.27 due
        to FSPS numerical accuracy  
        '''
        theta = self._parse_theta(tt)
        tt_zh = np.array([theta['gamma1_zh'], theta['gamma2_zh']])

        tburst = theta['tburst']

        if tburst > 13.27:
            warnings.warn('tburst > 13.27 Gyr returns 0s --- modify priors')
            return np.zeros(len(self._nmf_emu_waves))
        assert tburst > 1e-2, "burst currently only supported for tburst > 1e-2 Gyr"

        #dust1           = theta['dust1']
        dust2 = theta['dust2']
        dust_index = theta['dust_index']

        # get metallicity at tburst
        zburst = np.sum(np.array([tt_zh[i] * self._zh_basis[i](tburst)
                                  for i in range(self._N_nmf_zh)])).clip(self._Z_min, self._Z_max)

        # input to emulator are [tburst, zburst, dust2, dust_index]
        tt = np.array([
            tburst,
            [zburst],
            theta['dust2'],
            theta['dust_index']]).flatten()

        return self._emu_burst_nn(tt)

    def _emu_burst_nn(self, tt):
        ''' burst emulator neural network 
        '''
        tt = tt.astype(np.float32)

        logflux = []
        for iwave in range(self._burst_n_emu):  # wave bins
            W0, W1, W2, b0, b1, b2, alphas_, betas_, parameters_shift_, parameters_scale_,\
                pca_shift_, pca_scale_, spectrum_shift_, spectrum_scale_,\
                pca_transform_matrix_ = self._burst_emu_params[iwave]
            n_layers = self._burst_emu_nlayers[iwave]

            # PCA coefficients
            pca_emu = utils.pcaMLP(tt, W0, W1, W2, b0, b1, b2, alphas_, betas_,
                                   parameters_shift_, parameters_scale_,
                                   pca_shift_, pca_scale_, pca_transform_matrix_, n_layers)
            logflux.append(pca_emu * spectrum_scale_ + spectrum_shift_)
        return np.concatenate(logflux)

    def _load_emulator(self):
        ''' read in pickle files that contains the parameters for the FSPS
        emulator that is split into wavelength bins
        '''
        wbins = ['2000_3600', '3600_5500', '5500_7410', '7410_60000']
        # load NMF emulator
        npcas = [50, 50, 50, 30]

        def f_nn(npca, i): return 'nmf.v0.1.seed0_99.w%s.pca%i.8x256.nbatch250.pkl' % (
            wbins[i], npca)

        self._nmf_n_emu = len(npcas)
        self._nmf_emu_nlayers = []
        self._nmf_emu_params = []
        self._nmf_emu_wave = []

        for i, npca in enumerate(npcas):
            fpkl = open(os.path.join(
                os.path.dirname(os.path.realpath(__file__)), 'dat',
                f_nn(npca, i)), 'rb')
            W, b, alphas, betas, parameters_shift, parameters_scale,\
                pca_shift, pca_scale, spectrum_shift, spectrum_scale,\
                pca_transform_matrix, _, _, waves, _, _, nlayers, _ = pickle.load(
                    fpkl)

            # convert to float32 for future jit
            W0 = W[0].astype(np.float32)
            W1 = np.array(W[1:-1]).astype(np.float32)
            W2 = W[-1].astype(np.float32)

            b0 = b[0].astype(np.float32)
            b1 = np.array(b[1:-1]).astype(np.float32)
            b2 = b[-1].astype(np.float32)

            alphas = np.array(alphas).astype(np.float32)
            betas = np.array(betas).astype(np.float32)

            parameters_shift = parameters_shift.astype(np.float32)
            parameters_scale = parameters_scale.astype(np.float32)

            pca_shift = pca_shift.astype(np.float32)
            pca_scale = pca_scale.astype(np.float32)

            spectrum_shift = spectrum_shift.astype(np.float32)
            spectrum_scale = spectrum_scale.astype(np.float32)

            pca_transform_matrix = pca_transform_matrix.astype(np.float32)

            params = [W0, W1, W2, b0, b1, b2, alphas, betas, parameters_shift,
                      parameters_scale, pca_shift, pca_scale, spectrum_shift,
                      spectrum_scale, pca_transform_matrix]

            self._nmf_emu_nlayers.append(nlayers)
            self._nmf_emu_params.append(params)
            self._nmf_emu_wave.append(waves)

        self._nmf_emu_nlayers = np.array(self._nmf_emu_nlayers)
        self._nmf_emu_waves = np.concatenate(self._nmf_emu_wave)

        # load burst emulator
        def f_nn(npca, i): return 'burst.v0.1.seed0_199.w%s.pca%i.6x512.nbatch250.pkl' % (
            wbins[i], npca)
        self._burst_n_emu = len(npcas)
        self._burst_emu_nlayers = []
        self._burst_emu_params = []
        self._burst_emu_wave = []

        for i, npca in enumerate(npcas):
            fpkl = open(os.path.join(
                os.path.dirname(os.path.realpath(__file__)), 'dat',
                f_nn(npca, i)), 'rb')
            W, b, alphas, betas, parameters_shift, parameters_scale,\
                pca_shift, pca_scale, spectrum_shift, spectrum_scale,\
                pca_transform_matrix, _, _, waves, _, _, nlayers, _ = pickle.load(
                    fpkl)

            # convert to float32 for future jit
            W0 = W[0].astype(np.float32)
            W1 = np.array(W[1:-1]).astype(np.float32)
            W2 = W[-1].astype(np.float32)

            b0 = b[0].astype(np.float32)
            b1 = np.array(b[1:-1]).astype(np.float32)
            b2 = b[-1].astype(np.float32)

            alphas = np.array(alphas).astype(np.float32)
            betas = np.array(betas).astype(np.float32)

            parameters_shift = parameters_shift.astype(np.float32)
            parameters_scale = parameters_scale.astype(np.float32)

            pca_shift = pca_shift.astype(np.float32)
            pca_scale = pca_scale.astype(np.float32)

            spectrum_shift = spectrum_shift.astype(np.float32)
            spectrum_scale = spectrum_scale.astype(np.float32)

            pca_transform_matrix = pca_transform_matrix.astype(np.float32)

            params = [W0, W1, W2, b0, b1, b2, alphas, betas, parameters_shift,
                      parameters_scale, pca_shift, pca_scale, spectrum_shift,
                      spectrum_scale, pca_transform_matrix]

            self._burst_emu_nlayers.append(nlayers)
            self._burst_emu_params.append(params)
            self._burst_emu_wave.append(waves)

        self._burst_emu_nlayers = np.array(self._burst_emu_nlayers)
        self._burst_emu_waves = np.concatenate(self._burst_emu_wave)
        return None


class NMF_ZH(Model):
    '''
    Modified from provabgs.models.NMF. With metallicity history. 

    SPS model with non-parametric star formation history and flexible dust 
    attenuation model. The SFH is based on non-negative
    matrix factorization (NMF) bases (Tojeiro+in prep). The dust attenuation
    uses a standard Charlot & Fall dust model (in the language of FSPS, `dust_type=4`). 
    The IMF is Chabrier IMF.

    The SFH uses 4 NMF bases. If you specify `burst=True`, the SFH will
    include an additional burst component. 

    The metallicity here is absolute metallicity. Recall that Z_sun = 0.0190. 

    The dust attenuation is modeled using a 3 parameter Charlot & Fall model.
    `dust1` is tau_BC, the optical depth of dust attenuation of birth cloud
    that only affects young stellar population. `dust2` is tau_ISM, the optical
    depth of dust attenuation from the ISM, which affects all stellar emission.
    `dust_index` is the attenuation curve slope offset from Calzetti.

    If you specify `emulator=True`, the model will use a PCA NN emulator to
    evaluate the SPS model, rather than Flexible Stellar Population Synthesis.
    The emulator has <1% level accuracy and its *much* faster than FSPS. I
    recommend using the emulator for parameter exploration. 


    Parameters
    ----------
    burst : bool
        If True include a star bursts component in SFH. (default: True) 

    emulator : bool
        If True, use emulator rather than running FSPS. 

    cosmo : astropy.comsology object
        specify cosmology. If cosmo=None, NMF uses astorpy.cosmology.Planck15.

    peraa : bool
        IF True, the output spectrum will be in unit of L_sun/AA (per Angstrom).
        Otherwise, the output will be in unit of L_sun/Hz. (default: False).
        To convert from L_sun/Hz to L_sun/AA, multiply L_sun/Hz by lightspeed / wave**2.
        To convert L_sun/Hz to erg/s/cm^2/AA at 10 pc, multiply by `to_cgs_at_10pc`.

    Notes 
    -----
    * only supports 4 component SFH with or without burst and 2 component ZH 
    * only supports Calzetti+(2000) attenuation curve and Chabrier IMF. 
    * 2022-03-13: Emulator is not trained yet.
    '''

    def __init__(self, burst=True, emulator=False, cosmo=None, peraa=False):
        self._ssp = None
        self._burst = burst
        self._emulator = emulator
        self._peraa = peraa
        # metallicity range set by MIST isochrone
        self._Z_min = 4.49043431e-05
        self._Z_max = 4.49043431e-02
        super().__init__(cosmo=cosmo)  # initializes the model

    def _fsps(self, tt, tage):
        '''
        FSPS SED model. If `emulator=False`, FSPS is used to evaluate the
        SED rather than the emulator. First, SFH and ZH are constructed from
        the `tt` parameters. Then stellar population synthesis is used to get
        the spectra of each time bin. Afterwards, they're combined to construct
        the SED. 

        Parameters 
        ----------
        tt : 1d array 
            Nparam array that specifies the parameter values 

        tage : float 
            age of galaxy 


        Returns
        -------
        wave_rest : array_like[Nwave] 
            rest-frame wavelength of SSP flux 


        lum_ssp : array_like[Nwave] 
            FSPS SSP luminosity in units of Lsun/A

        Notes
        -----
        * 12/23/2020: age of SSPs are no longer set to the center of the
          lookback time as this  ignores the youngest tage ~ 0 SSP, which have
          significant contributions. Also we set a minimum tage = 1e-8 because
          FSPS returns a grid for tage = 0 but 1e-8 gets the youngest isochrone
          anyway. 
        * 2021/06/24: log-spaced lookback time implemented
        '''

        if self._ssp is None:
            self._ssp_initiate()  # initialize FSPS StellarPopulation object
        theta = self._parse_theta(tt)

        assert np.isclose(np.sum([theta['beta1_sfh'], theta['beta2_sfh'],
                                  theta['beta3_sfh'], theta['beta4_sfh']],
                                 axis=0), 1.).all(), "SFH basis coefficients should add up to 1"

        # NMF SFH(t) noramlized to 1 **without burst**
        tlb_edges, sfh = self.SFH(np.hstack(
            [np.zeros(1), tt[1:]]), tage=tage,
            _burst=False)
        # Replace whatever stellar mass with 1 Msun
        # 0.0 stands for logmstar = 0.0, i.e., normalized to 1 M_sun
        # NMF ZH at lookback time bins
        _, zh = self.ZH(tt, tage=tage)

        tages = 0.5 * (tlb_edges[1:] + tlb_edges[:-1])  # ages of SSP
        dt = np.diff(tlb_edges)  # bin widths

        # look over log-spaced lookback time bins and add up SSPs
        for i, tage in enumerate(tages):
            m = 1e9 * dt[i] * sfh[i]  # mass formed in this bin
            if m == 0 and i != 0:
                continue

            self._ssp.params['logzsol'] = np.log10(
                zh[i] / 0.0190)  # log(Z/Zsun)
            self._ssp.params['dust1'] = theta['dust1']
            self._ssp.params['dust2'] = theta['dust2']
            self._ssp.params['dust_index'] = theta['dust_index']

            wave_rest, lum_i = self._ssp.get_spectrum(
                tage=tage, peraa=self._peraa)
            # If peraa=True, the spectrum will be in units of Lsun/AA
            # note that this spectrum is normalized such that the total formed
            # mass = 1 Msun

            if i == 0:
                lum_ssp = np.zeros(len(wave_rest))
            lum_ssp += m * lum_i

        # add burst contribution
        if self._burst:
            fburst = theta['fburst']
            tburst = theta['tburst']

            lum_burst = np.zeros(lum_ssp.shape)
            # if starburst is within the age of the galaxy
            if tburst < tage:
                _, lum_burst = self._fsps_burst(tt)

            # renormalize NMF contribution
            lum_ssp *= (1. - fburst)

            # add in burst contribution
            lum_ssp += fburst * lum_burst

        # normalize by stellar mass
        lum_ssp *= (10**theta['logmstar'])

        return wave_rest, lum_ssp

    def _fsps_burst(self, tt, debug=False):
        '''
        Dust attenuated spectra of single stellar population that
        corresponds to the single star-burst event. The spectrum is normalized 
        such that the total formed mass is 1 Msun, **not** fburst. Therefore 
        when adding this burst component back, `fburst` should be multiplied. 
        The spectrum is calculated using FSPS. 
        '''
        if self._ssp is None:
            self._ssp_initiate()  # initialize FSPS StellarPopulation object
        theta = self._parse_theta(tt)

        tburst = theta['tburst']
        assert tburst > 1e-2, "burst currently only supported for tburst > 1e-2 Gyr = 10 Myr"

        # get metallicity at tburst
        tt_zh = np.array([theta['gamma1_zh'], theta['gamma2_zh']])
        zburst = np.sum(np.array([tt_zh[i] * self._zh_basis[i](tburst)
                                  for i in range(self._N_nmf_zh)])).clip(self._Z_min, self._Z_max)
        if debug:
            print('zburst=%e' % zburst)
            print('dust2=%f' % dust2)
            print('dust_index=%f' % dust_index)

        # luminosity of SSP at tburst
        # np.log10(zburst/0.0190)  # log(Z/Zsun)
        self._ssp.params['logzsol'] = np.log10(zburst / 0.0190)  # log(Z/Zsun)
        self._ssp.params['dust1'] = 0.  # no birth cloud attenuation for tage > 1e-2 Gyr
        self._ssp.params['dust2'] = theta['dust2']
        self._ssp.params['dust_index'] = theta['dust_index']

        wave_rest, lum_burst = self._ssp.get_spectrum(
            tage=tburst, peraa=self._peraa)
        # If peraa=True, the spectrum will be in units of Lsun/AA
        # note that this spectrum is normalized such that the total formed
        # mass = 1 Msun
        return wave_rest, lum_burst

    def SFH(self, tt, zred=None, tage=None, _burst=True):
        ''' star formation history for given set of parameter values and
        redshift.

        Parameters
        ----------
        tt : 1d or 2d array 
            N-param or NxN param array of parameter values 

        tage : float
            age of the galaxy 

        Returns
        -------
        tedges: 1d array 
            bin edges of log-spaced look back time

        sfh : 1d or 2d array 
            star formation history in Msun/yr
        '''
        if zred is None and tage is None:
            raise ValueError(
                "specify either the redshift or age of the galaxy")
        if tage is None:
            assert isinstance(zred, float)
            tage = self.cosmo.age(zred).value  # age in Gyr

        theta = self._parse_theta(tt)

        # sfh nmf basis coefficients
        tt_sfh = np.array([theta['beta1_sfh'], theta['beta2_sfh'],
                           theta['beta3_sfh'], theta['beta4_sfh']]).T

        # log-spaced lookback time bin edges
        tlb_edges = utils.tlookback_bin_edges(tage)

        sfh_basis_tlb = np.array([
            utils.trapz_rebin(self._t_lb_hr, _sfh_basis, edges=tlb_edges)
            for _sfh_basis in self._sfh_basis_hr])

        sfh = np.sum(np.array([tt_sfh[:, i][:, None] *
                               sfh_basis_tlb[i][None, :] for i in range(self._N_nmf_sfh)]),
                     axis=0)

        sfh /= np.sum(1e9 * np.diff(tlb_edges) * sfh,
                      axis=1)[:, None]  # normalize

        # add starburst
        if self._burst and _burst:
            # fraction of stellar mass from star burst
            fburst = theta['fburst']
            tburst = theta['tburst']  # time of star burst

            noburst = (tburst > tage)
            fburst[noburst] = 0.

            # add normalized starburst to SFH
            sfh *= (1. - fburst)[:, None]
            sfh += fburst[:, None] * self._single_SFH_burst(tburst, tlb_edges)

        # multiply by stellar mass
        sfh *= 10**theta['logmstar'][:, None]

        if np.atleast_2d(tt).shape[0] == 1:
            return tlb_edges, sfh[0]
        return tlb_edges, sfh

    def _single_SFH_burst(self, tburst, tedges):
        ''' place a single star-burst event on the SFH
        '''
        tburst = np.atleast_1d(tburst)
        dts = np.diff(tedges)

        # burst within the age of the galaxy
        has_burst = (tburst < tedges.max())

        # log-spaced lookback time bin with burst
        iburst = np.digitize(tburst[has_burst], tedges) - 1

        sfh = np.zeros((len(tburst), len(tedges) - 1))
        sfh[has_burst, iburst] += 1. / (1e9 * dts[iburst])
        return sfh

    def avgSFR(self, tt, zred=None, tage=None, dt=1):
        ''' given a set of parameter values `tt` and redshift `zred`, calculate
        SFR averaged over `dt` Gyr. 

        parameters
        ----------
        tt : array_like[Ntheta, Nparam]
           Parameter values of [log M*, b1SFH, b2SFH, b3SFH, b4SFH, g1ZH, g2ZH,
           'dust1', 'dust2', 'dust_index']. 

        zred : float 
            redshift

        tage : float 
            age of galaxy 

        dt : float
            Gyrs to average the SFHs 
        '''
        if zred is None and tage is None:
            raise ValueError('specify either zred or tage')
        if zred is not None and tage is not None:
            raise ValueError('specify either zred or tage')
        if tage is None:
            assert isinstance(zred, float)
            tage = self.cosmo.age(zred).value  # age in Gyr

        theta = self._parse_theta(tt)

        # sfh nmf basis coefficients
        tt_sfh = np.array([theta['beta1_sfh'], theta['beta2_sfh'],
                           theta['beta3_sfh'], theta['beta4_sfh']]).T

        # log-spaced lookback time bin edges
        tlb_edges = utils.tlookback_bin_edges(tage)
        assert dt < tlb_edges[-1]

        sfh_basis_tlb = np.array([
            utils.trapz_rebin(self._t_lb_hr, _sfh_basis, edges=tlb_edges)
            for _sfh_basis in self._sfh_basis_hr])

        sfh = np.sum(np.array([tt_sfh[:, i][:, None] *
                               sfh_basis_tlb[i][None, :] for i in range(self._N_nmf_sfh)]),
                     axis=0)

        sfh /= np.sum(np.diff(tlb_edges) * sfh, axis=1)[:, None]  # normalize

        # calculate stellar mass formed over 0-dt
        i_dt = np.digitize(dt, tlb_edges) - 1
        Mform = np.sum(np.diff(tlb_edges)[
                       :i_dt][None, :] * sfh[:, :i_dt], axis=1)
        Mform += (dt - tlb_edges[i_dt]) * sfh[:, i_dt]

        # add starburst event
        if self._burst:
            # fraction of stellar mass from star burst
            fburst = theta['fburst']
            tburst = theta['tburst']  # time of star burst

            noburst = (tburst > tage)
            fburst[noburst] = 0.
            Mform *= (1. - fburst)

            burst_dt = (tburst < dt)
            Mform[burst_dt] += theta['fburst'][burst_dt]

        # multiply by stellar mass
        avg_sfr = Mform * 10**theta['logmstar'] / dt / 1e9
        return avg_sfr

    def ZH(self, tt, zred=None, tage=None):
        ''' metallicity history for given set of parameters. metallicity is
        parameterized using a 2 component NMF basis. The parameter values
        specify the coefficient of the components and this method returns the
        linear combination of the two. 

        parameters
        ----------
        tt : array_like[N,Nparam]
           parameter values of the model 

        zred : float
            redshift of galaxy/csp

        Returns 
        -------
        tedge : 1d array
            bin edges of lookback time

        zh : 2d array
            metallicity at cosmic time t --- ZH(t) 
        '''
        if zred is None and tage is None:
            raise ValueError(
                "specify either the redshift or age of the galaxy")
        if tage is None:
            assert isinstance(zred, float)
            tage = self.cosmo.age(zred).value  # age in Gyr
        theta = self._parse_theta(tt)

        # metallicity history basis coefficients
        tt_zh = np.array([theta['gamma1_zh'], theta['gamma2_zh']]).T

        # log-spaced lookback time bin edges
        tlb_edges = utils.tlookback_bin_edges(tage)
        tlb = 0.5 * (tlb_edges[1:] + tlb_edges[:-1])

        # metallicity basis
        _z_basis = np.array([self._zh_basis[i](tlb)
                            for i in range(self._N_nmf_zh)])

        # get metallicity history
        zh = np.sum(np.array([tt_zh[:, i][:, None] * _z_basis[i][None, :] for i in
                              range(self._N_nmf_zh)]), axis=0).clip(self._Z_min, self._Z_max)

        if tt_zh.shape[0] == 1:
            return tlb_edges, zh[0]
        return tlb_edges, zh

    def Z_MW(self, tt, tage=None, zred=None):
        ''' given theta calculate mass weighted metallicity using the ZH NMF
        bases. 
        '''
        if tage is None and zred is None:
            raise ValueError("specify either zred or tage")
        if tage is not None and zred is not None:
            raise ValueError("specify either zred or tage")

        theta = self._parse_theta(tt)
        tlb_edge, sfh = self.SFH(tt, tage=tage, zred=zred)  # get SFH
        _, zh = self.ZH(tt, tage=tage, zred=zred)

        # mass weighted average
        z_mw = np.sum(1e9 * np.diff(tlb_edge)
                      [None, :] * sfh * zh, axis=1) / (10**theta['logmstar'])
        return z_mw

    def tage_MW(self, tt, tage=None, zred=None):
        ''' given theta calculate mass weighted age of the galaxy 
        '''
        if tage is None and zred is None:
            raise ValueError("specify either zred or tage")
        if tage is not None and zred is not None:
            raise ValueError("specify either zred or tage")

        theta = self._parse_theta(tt)
        tlb_edge, sfh = self.SFH(tt, tage=tage, zred=zred)  # get SFH
        th = 0.5 * (tlb_edge[1:] + tlb_edge[:-1])

        # mass weighted average
        t_mw = np.sum(1e9 * np.diff(tlb_edge)
                      [None, :] * sfh * th, axis=1) / (10**theta['logmstar'])
        return t_mw

    def _load_NMF_bases(self, name='tojeiro.4comp'):
        ''' read in NMF SFH and ZH bases. These bases are used to reduce the
        dimensionality of the SFH and ZH. 
        '''
        dir_dat = os.path.join(os.path.dirname(
            os.path.realpath(__file__)), 'dat')
        if name == 'tojeiro.4comp':
            fsfh = os.path.join(
                dir_dat, 'NMF_2basis_SFH_components_nowgt_lin_Nc4.txt')
            fzh = os.path.join(
                dir_dat, 'NMF_2basis_Z_components_nowgt_lin_Nc2.txt')
            ft = os.path.join(dir_dat, 'sfh_t_int.txt')

            # basis order is jumbled up it should be [2 ,0, 1, 3]
            nmf_sfh = np.loadtxt(fsfh)[:, ::-1]
            nmf_zh = np.loadtxt(fzh)[:, ::-1]
            nmf_t = np.loadtxt(ft)[::-1]  # look back time

            self._nmf_t_lb_sfh = nmf_t
            self._nmf_t_lb_zh = nmf_t
            self._nmf_sfh_basis = np.array(
                [nmf_sfh[2], nmf_sfh[0], nmf_sfh[1], nmf_sfh[3]])
            self._nmf_zh_basis = nmf_zh
        elif name in ['tng.4comp', 'tng.6comp']:
            raise NotImplementedError('TNG is not implemented yet')
            # icomp = int(name.split('.')[-1][0])
            # fsfh = os.path.join(dir_dat, 'NMF_basis.sfh.tng%icomp.txt' % icomp)
            # fzh = os.path.join(
            #     dir_dat, 'NMF_2basis_Z_components_nowgt_lin_Nc2.txt')
            # ftsfh = os.path.join(dir_dat, 't_sfh.tng%icomp.txt' % icomp)
            # ftzh = os.path.join(dir_dat, 'sfh_t_int.txt')

            # nmf_sfh = np.loadtxt(fsfh, unpack=True)
            # nmf_zh = np.loadtxt(fzh)
            # nmf_tsfh = np.loadtxt(ftsfh)  # look back time
            # nmf_tzh = np.loadtxt(ftzh)  # look back time

            # self._nmf_t_lb_sfh = nmf_tsfh
            # self._nmf_t_lb_zh = nmf_tzh[::-1]
            # self._nmf_sfh_basis = nmf_sfh
            # self._nmf_zh_basis = nmf_zh[:, ::-1]
        else:
            raise NotImplementedError

        self._Ncomp_sfh = self._nmf_sfh_basis.shape[0]
        self._Ncomp_zh = self._nmf_zh_basis.shape[0]

        # SFH bases as a function of lookback time
        self._sfh_basis = [
            Interp.InterpolatedUnivariateSpline(
                self._nmf_t_lb_sfh,
                self._nmf_sfh_basis[i], k=1)
            for i in range(self._Ncomp_sfh)
        ]
        self._zh_basis = [
            Interp.InterpolatedUnivariateSpline(
                self._nmf_t_lb_zh,
                self._nmf_zh_basis[i], k=1)
            for i in range(self._Ncomp_zh)]

        # high resolution tabulated SFHs used in the SFH calculation
        self._t_lb_hr = np.linspace(0., self.age_univ, int(5e4))
        self._sfh_basis_hr = [sfh_basis(self._t_lb_hr)
                              for sfh_basis in self._sfh_basis]
        return None

    def _init_model(self, **kwargs):
        ''' some under the hood initalization of the model and its
        parameterization. 
        '''
        # load 4 component NMF bases from Rita
        self._load_NMF_bases(name='tojeiro.4comp')
        self._N_nmf_sfh = 4  # 4 NMF component SFH
        self._N_nmf_zh = 2

        if not self._burst:
            self._parameters = [
                'logmstar',
                'beta1_sfh',
                'beta2_sfh',
                'beta3_sfh',
                'beta4_sfh',
                'gamma1_zh',
                'gamma2_zh',
                'dust1',
                'dust2',
                'dust_index']
        else:
            self._parameters = [
                'logmstar',
                'beta1_sfh',
                'beta2_sfh',
                'beta3_sfh',
                'beta4_sfh',
                'fburst',
                # lookback time of the universe when burst occurs (tburst < tage)
                'tburst',
                'gamma1_zh',
                'gamma2_zh',
                'dust1',
                'dust2',
                'dust_index']

        if not self._emulator:
            self._sps_model = self._fsps
        else:
            self._sps_model = self._emu
            self._load_emulator()

        return None

    def _ssp_initiate(self):
        ''' initialize sps (FSPS StellarPopulaiton object) 
        '''
        sfh = 0  # tabulated SFH
        dust_type = 4  # dust1, dust2, and dust_index
        imf_type = 1  # chabrier

        self._ssp = fsps.StellarPopulation(
            zcontinuous=1,          # interpolate metallicities
            sfh=sfh,                # sfh type
            dust_type=dust_type,    # dust model type
            imf_type=imf_type)      # chabrier
        return None

    def _emu(self, tt, tage):
        ''' PCA neural network emulator of FSPS SED model. If `emulator=True`,
        this emulator is used instead of `_fsps`. The emulator is *much* faster
        than the FSPS.

        Parameters
        ----------
        tt : 1d array
            Nparam array that specifies the parameter values

        tage : float
            age of galaxy

        Returns
        -------
        wave_rest : array_like[Nwave]
            rest-frame wavelength of SSP flux

        lum_ssp : array_like[Nwave]
            FSPS SSP luminosity in units of Lsun/A

        Notes
        -----
        * June 11, 2021: burst component no longer uses an emulator because
            it's fast enough.
        '''

        theta = self._parse_theta(tt)

        assert np.isclose(np.sum([theta['beta1_sfh'], theta['beta2_sfh'],
                                  theta['beta3_sfh'], theta['beta4_sfh']]), 1.), "SFH basis coefficients should add up to 1"

        # get redshift with interpolation
        #zred = self._z_tage_interp(tage)

        tt_nmf = np.concatenate([theta['beta1_sfh'], theta['beta2_sfh'],
                                 theta['beta3_sfh'], theta['beta4_sfh'], theta['gamma1_zh'],
                                 theta['gamma2_zh'], theta['dust1'], theta['dust2'],
                                 theta['dust_index'], [tage]])  # zred]])

        assert theta['gamma2_zh'] < 2.0e-2
        assert theta['gamma1_zh'] > 4.5e-5
        # NMF from emulator
        lum_ssp = np.exp(self._emu_nmf(tt_nmf))

        # add burst contribution
        if self._burst:
            fburst = theta['fburst']
            tburst = theta['tburst']

            lum_burst = np.zeros(lum_ssp.shape)
            # if starburst is within the age of the galaxy
            if tburst < tage and fburst > 0.:
                lum_burst = np.exp(self._emu_burst(tt))
                #_w, _lum_burst = self._fsps_burst(tt)
                #lum_burst = _lum_burst[(_w > 2300.) & (_w < 60000.)]

            # renormalize NMF contribution
            lum_ssp *= (1. - fburst)

            # add in burst contribution
            lum_ssp += fburst * lum_burst

        # normalize by stellar mass
        lum_ssp *= (10**theta['logmstar'])

        return self._nmf_emu_waves, lum_ssp

    def _emu_nmf(self, tt):
        ''' emulator for the SED from the NMF star formation history.

        Parameters
        ----------
        tt : 1d array 
            Nparam array that specifies 
            [beta1_sfh, beta2_sfh, beta3_sfh, beta4_sfh, gamma1_zh, gamma2_zh, dust1, dust2, dust_index, redshift] 

        Returns
        -------
        logflux : array_like[Nwave,] 
            (natural) log of (SSP luminosity in units of Lsun/A)
        '''
        # untransform SFH coefficients from Dirichlet distribution
        _tt = np.empty(9)
        _tt[0] = (1. - tt[0]).clip(1e-8, None)
        for i in range(1, 3):
            _tt[i] = 1. - (tt[i] / np.prod(_tt[:i]))
        _tt[3:] = tt[4:]
        _tt = _tt.astype(np.float32)

        logflux = []
        for iwave in range(self._nmf_n_emu):  # wave bins

            W0, W1, W2, b0, b1, b2, alphas_, betas_, param_shift, param_scale,\
                pca_shift_, pca_scale_, spectrum_shift_, spectrum_scale_,\
                pca_transform_matrix_ = self._nmf_emu_params[iwave]
            n_layers = self._nmf_emu_nlayers[iwave]

            # PCA coefficients
            pca_emu = utils.pcaMLP(_tt, W0, W1, W2, b0, b1, b2, alphas_, betas_,
                                   param_shift, param_scale, pca_shift_, pca_scale_,
                                   pca_transform_matrix_, n_layers)
            logflux.append(pca_emu * spectrum_scale_ + spectrum_shift_)

        return np.concatenate(logflux)

    def _emu_burst(self, tt, debug=False):
        ''' calculate the dust attenuated luminosity contribution from a SSP
        that corresponds to the burst using an emulator. This spectrum is
        normalized such that the total formed mass is 1 Msun, **not** fburst 

        Notes
        -----
        * currently luminosity contribution is set to 0 if tburst > 13.27 due
        to FSPS numerical accuracy  
        '''
        theta = self._parse_theta(tt)
        tt_zh = np.array([theta['gamma1_zh'], theta['gamma2_zh']])

        tburst = theta['tburst']

        if tburst > 13.27:
            warnings.warn('tburst > 13.27 Gyr returns 0s --- modify priors')
            return np.zeros(len(self._nmf_emu_waves))
        assert tburst > 1e-2, "burst currently only supported for tburst > 1e-2 Gyr"

        #dust1           = theta['dust1']
        dust2 = theta['dust2']
        dust_index = theta['dust_index']

        # get metallicity at tburst
        zburst = np.sum(np.array([tt_zh[i] * self._zh_basis[i](tburst)
                                  for i in range(self._N_nmf_zh)])).clip(self._Z_min, self._Z_max)

        # input to emulator are [tburst, zburst, dust2, dust_index]
        tt = np.array([
            tburst,
            [zburst],
            theta['dust2'],
            theta['dust_index']]).flatten()

        return self._emu_burst_nn(tt)

    def _emu_burst_nn(self, tt):
        ''' burst emulator neural network 
        '''
        tt = tt.astype(np.float32)

        logflux = []
        for iwave in range(self._burst_n_emu):  # wave bins
            W0, W1, W2, b0, b1, b2, alphas_, betas_, parameters_shift_, parameters_scale_,\
                pca_shift_, pca_scale_, spectrum_shift_, spectrum_scale_,\
                pca_transform_matrix_ = self._burst_emu_params[iwave]
            n_layers = self._burst_emu_nlayers[iwave]

            # PCA coefficients
            pca_emu = utils.pcaMLP(tt, W0, W1, W2, b0, b1, b2, alphas_, betas_,
                                   parameters_shift_, parameters_scale_,
                                   pca_shift_, pca_scale_, pca_transform_matrix_, n_layers)
            logflux.append(pca_emu * spectrum_scale_ + spectrum_shift_)
        return np.concatenate(logflux)

    def _load_emulator(self):
        ''' read in pickle files that contains the parameters for the FSPS
        emulator that is split into wavelength bins
        '''
        wbins = ['2000_3600', '3600_5500', '5500_7410', '7410_60000']
        # load NMF emulator
        npcas = [50, 50, 50, 30]

        def f_nn(npca, i): return 'nmf.v0.1.seed0_99.w%s.pca%i.8x256.nbatch250.pkl' % (
            wbins[i], npca)

        self._nmf_n_emu = len(npcas)
        self._nmf_emu_nlayers = []
        self._nmf_emu_params = []
        self._nmf_emu_wave = []

        for i, npca in enumerate(npcas):
            fpkl = open(os.path.join(
                os.path.dirname(os.path.realpath(__file__)), 'dat',
                f_nn(npca, i)), 'rb')
            W, b, alphas, betas, parameters_shift, parameters_scale,\
                pca_shift, pca_scale, spectrum_shift, spectrum_scale,\
                pca_transform_matrix, _, _, waves, _, _, nlayers, _ = pickle.load(
                    fpkl)

            # convert to float32 for future jit
            W0 = W[0].astype(np.float32)
            W1 = np.array(W[1:-1]).astype(np.float32)
            W2 = W[-1].astype(np.float32)

            b0 = b[0].astype(np.float32)
            b1 = np.array(b[1:-1]).astype(np.float32)
            b2 = b[-1].astype(np.float32)

            alphas = np.array(alphas).astype(np.float32)
            betas = np.array(betas).astype(np.float32)

            parameters_shift = parameters_shift.astype(np.float32)
            parameters_scale = parameters_scale.astype(np.float32)

            pca_shift = pca_shift.astype(np.float32)
            pca_scale = pca_scale.astype(np.float32)

            spectrum_shift = spectrum_shift.astype(np.float32)
            spectrum_scale = spectrum_scale.astype(np.float32)

            pca_transform_matrix = pca_transform_matrix.astype(np.float32)

            params = [W0, W1, W2, b0, b1, b2, alphas, betas, parameters_shift,
                      parameters_scale, pca_shift, pca_scale, spectrum_shift,
                      spectrum_scale, pca_transform_matrix]

            self._nmf_emu_nlayers.append(nlayers)
            self._nmf_emu_params.append(params)
            self._nmf_emu_wave.append(waves)

        self._nmf_emu_nlayers = np.array(self._nmf_emu_nlayers)
        self._nmf_emu_waves = np.concatenate(self._nmf_emu_wave)

        # load burst emulator
        def f_nn(npca, i): return 'burst.v0.1.seed0_199.w%s.pca%i.6x512.nbatch250.pkl' % (
            wbins[i], npca)
        self._burst_n_emu = len(npcas)
        self._burst_emu_nlayers = []
        self._burst_emu_params = []
        self._burst_emu_wave = []

        for i, npca in enumerate(npcas):
            fpkl = open(os.path.join(
                os.path.dirname(os.path.realpath(__file__)), 'dat',
                f_nn(npca, i)), 'rb')
            W, b, alphas, betas, parameters_shift, parameters_scale,\
                pca_shift, pca_scale, spectrum_shift, spectrum_scale,\
                pca_transform_matrix, _, _, waves, _, _, nlayers, _ = pickle.load(
                    fpkl)

            # convert to float32 for future jit
            W0 = W[0].astype(np.float32)
            W1 = np.array(W[1:-1]).astype(np.float32)
            W2 = W[-1].astype(np.float32)

            b0 = b[0].astype(np.float32)
            b1 = np.array(b[1:-1]).astype(np.float32)
            b2 = b[-1].astype(np.float32)

            alphas = np.array(alphas).astype(np.float32)
            betas = np.array(betas).astype(np.float32)

            parameters_shift = parameters_shift.astype(np.float32)
            parameters_scale = parameters_scale.astype(np.float32)

            pca_shift = pca_shift.astype(np.float32)
            pca_scale = pca_scale.astype(np.float32)

            spectrum_shift = spectrum_shift.astype(np.float32)
            spectrum_scale = spectrum_scale.astype(np.float32)

            pca_transform_matrix = pca_transform_matrix.astype(np.float32)

            params = [W0, W1, W2, b0, b1, b2, alphas, betas, parameters_shift,
                      parameters_scale, pca_shift, pca_scale, spectrum_shift,
                      spectrum_scale, pca_transform_matrix]

            self._burst_emu_nlayers.append(nlayers)
            self._burst_emu_params.append(params)
            self._burst_emu_wave.append(waves)

        self._burst_emu_nlayers = np.array(self._burst_emu_nlayers)
        self._burst_emu_waves = np.concatenate(self._burst_emu_wave)
        return None
