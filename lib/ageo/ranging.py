"""ageo.ranging - active geolocation library: ranging functions.

A ranging function computes (non-normalized) probability as a function
of geographic location, given a reference location, calibration data
(see ageo.calibration), and a set of timing observations.  This module
provides several different algorithms for this calculation.
"""

import numpy as np
import pyproj
from shapely.geometry import Point
from shapely.ops import transform as sh_transform
from functools import partial
from sys import stderr
from scipy import interpolate, stats

from .calibration import PhysicalLimitsOnly

WGS84_globe = pyproj.Proj(proj='latlong', ellps='WGS84')

def Disk(x, y, radius):
    return Point(x, y).buffer(radius)

# Convenience wrappers for forward and inverse geodetic computations
# on the WGS84 ellipsoid, smoothing over some warts in pyproj.Geod.
# inv() and fwd() take coordinates in lon/lat order and distances in
# meters, whereas the rest of this program uses lat/lon order and
# distances in kilometers.  They are vectorized internally, but do not
# support numpy-style broadcasting.  The prebound _Fwd, _Inv, and
# _Bcast are strictly performance hacks.
_WGS84geod = pyproj.Geod(ellps='WGS84')
def WGS84dist(lat1, lon1, lat2, lon2, *,
              _Inv = _WGS84geod.inv, _Bcast = np.broadcast_arrays):
    _, _, dist = _Inv(*_Bcast(lon1, lat1, lon2, lat2))
    return dist/1000
def WGS84loc(lat, lon, az, dist, *,
             _Fwd = _WGS84geod.fwd, _Bcast = np.broadcast_arrays):
    tlon, tlat, _ = _Fwd(*_Bcast(lon, lat, az, dist*1000))
    return tlat, tlon

# half of the equatorial circumference of the Earth, in meters
# it is impossible for the target to be farther away than this
DISTANCE_LIMIT = 20037508

# PhysicalLimitsOnly instances are data-independent, so we only need two
PHYSICAL_BOUNDS  = PhysicalLimitsOnly('physical')
EMPIRICAL_BOUNDS = PhysicalLimitsOnly('empirical')

class RangingFunction:
    """Abstract base class."""

    def __init__(self, calibration, rtts, fuzz):
        self.calibration = calibration
        self.rtts = rtts
        self.fuzz = fuzz

    def unnormalized_pvals(self, distances):
        raise NotImplementedError

    def distance_bound(self):
        raise NotImplementedError

class MinMax(RangingFunction):
    """An _ideal_ min-max ranging function is a flat nonzero value
       for any distance in between the minimum and maximum distances
       considered feasible by the calibration, and 0 otherwise.

       Because all of the empirical calibration algorithms are liable
       to spit out an observation from time to time that's
       inconsistent with the global truth, we do not drop the probability
       straight to zero immediately at the limits suggested by the
       calibration.  Instead, we make it fall off linearly to the bounds
       given by PHYSICAL_BOUNDS, with a knee at EMPIRICAL_BOUNDS.
    """

    def __init__(self, *args, **kwargs):
        RangingFunction.__init__(self, *args, **kwargs)
        min_cal, max_cal = \
            self.calibration.distance_range(self.rtts)
        min_emp, max_emp = EMPIRICAL_BOUNDS.distance_range(self.rtts)
        min_phy, max_phy = PHYSICAL_BOUNDS.distance_range(self.rtts)
        self.bounds = [
            min(DISTANCE_LIMIT, max(0, val))
            for val in
            (min_cal, max_cal, min_emp, max_emp, min_phy, max_phy)]
        self.bounds.sort()

        self.interpolant = interpolate.interp1d(
            self.bounds,
            [0, .75, 1, 1, .75, 0],
            kind = 'linear',
            fill_value = 0,
            bounds_error = False
        )

    def distance_bound(self):
        return self.bounds[-1]

    def unnormalized_pvals(self, dist):
        return self.interpolant(dist)

class Gaussian(RangingFunction):
    """A Gaussian ranging function is simply the pdf of a normal
       distribution with mean and standard deviation given by the
       calibration.

       Right now it only makes sense to use ranging.Gaussian with
       calibration.Spotter, and this class has intimate knowledge
       of calibration.Spotter's internals.

       For the reasons discussed above, the outer distance bound for
       this function is the PHYSICAL_BOUNDS distance bound, and this
       is also used as a "clip" on the pdf (which is nonzero
       everywhere).
    """

    def __init__(self, *args, **kwargs):
        RangingFunction.__init__(self, *args, **kwargs)
        if not hasattr(self.calibration, '_mu') \
           or not hasattr(self.calibration, '_sigma'):
            raise TypeError("Gaussian ranging function requires a "
                            "calibration with _mu and _sigma")
        min_cal, max_cal = \
            self.calibration.distance_range(self.rtts)
        min_phy, max_phy = PHYSICAL_BOUNDS.distance_range(self.rtts)

        self._distance_bound = max(min_cal, max_cal, min_phy, max_phy)

        # FIXME: this logic belongs in calibration.Spotter.
        med_rtt = np.percentile(self.rtts, .25)
        mu = self.calibration._mu(med_rtt)
        sigma = self.calibration._sigma(med_rtt)
        # mu and sigma can go negative when two nodes are very close together,
        # which causes stats.norm.pdf() to spit out nothing but NaN.
        mu = max(mu, 1000)         # lower bound at 1km
        sigma = max(sigma, 1000/3) # lower bound at 3sigma=1km
        self._distribution = stats.norm(
            loc   = mu,
            scale = sigma
        )

    def distance_bound(self):
        return self._distance_bound

    def unnormalized_pvals(self, dist):
        rv = self._distribution.pdf(dist)
        if not np.isfinite(rv).all():
            ve = ValueError("pdf returned non-finite values")
            ve.req_domain = dist
            ve.req_range = rv
            ve.range_fn = self
            raise ve

        rv[dist > self._distance_bound] = 0
        return rv
