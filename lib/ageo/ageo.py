"""ageo - active geolocation library: core.
"""

__all__ = ('Location', 'Map', 'Observation')

import bisect
import functools
import itertools
import numpy as np
import pyproj
from scipy import sparse
from shapely.geometry import Point, MultiPoint, Polygon, box as Box
from shapely.ops import transform as sh_transform
import tables
import math
import sys

# scipy.sparse.find() materializes vectors which, in several cases
# below, can be enormous.  This is slower, but more memory-efficient.
# Code from https://stackoverflow.com/a/31244368/388520 with minor
# modifications.
def iter_csr_nonzero(matrix):
    irepeat = itertools.repeat
    return zip(
        # reconstruct the row indices
        itertools.chain.from_iterable(
            irepeat(i, r)
            for (i,r) in enumerate(matrix.indptr[1:] - matrix.indptr[:-1])
        ),
        # matrix.indices gives the column indices as-is
        matrix.indices,
        matrix.data
    )

def Disk(x, y, radius):
    return Point(x, y).buffer(radius)

# Important note: pyproj consistently takes coordinates in lon/lat
# order and distances in meters.  lon/lat order makes sense for
# probability matrices, because longitudes are horizontal = columns,
# latitudes are vertical = rows, and scipy matrices are column-major
# (blech).  Therefore, this library also consistently uses lon/lat
# order and meters.

# Coordinate transformations used by Location.centroid()
wgs_proj  = pyproj.Proj("+proj=longlat +ellps=WGS84 +datum=WGS84 +no_defs")
gcen_proj = pyproj.Proj("+proj=geocent +datum=WGS84 +units=m +no_defs")
wgs_to_gcen = functools.partial(pyproj.transform, wgs_proj, gcen_proj)
gcen_to_wgs = functools.partial(pyproj.transform, gcen_proj, wgs_proj)

# ... and Location.area()
cea_proj   = pyproj.Proj(proj='cea', ellps='WGS84', lon_0=0, lat_ts=0)
wgs_to_cea = functools.partial(pyproj.transform, wgs_proj, cea_proj)

# Smooth over warts in pyproj.Geod.inv(), which is vectorized
# internally, but does not support numpy-style broadcasting, and
# returns things we don't need.  The prebound _Inv and _Bcast are
# strictly performance hacks.
_WGS84geod = pyproj.Geod(ellps='WGS84')
def WGS84dist(lon1, lat1, lon2, lat2, *,
              _Inv = _WGS84geod.inv, _Bcast = np.broadcast_arrays):
    _, _, dist = _Inv(*_Bcast(lon1, lat1, lon2, lat2))
    return dist

def cartesian2(a, b):
    """Cartesian product of two 1D vectors A and B."""
    return np.tile(a, len(b)), np.repeat(b, len(a))

def mask_ij(bounds, longitudes, latitudes):
    """Given a rectangle-tuple BOUNDS (west, south, east, north; as
       returned by shapely .bounds properties), and sorted grid index
       vectors LONGITUDES, LATITUDES, return vectors I, J which give the
       x- and y-indices of every grid point within the rectangle.
       LATITUDES and LONGITUDES must be sorted.
    """
    try:
        (west, south, east, north) = bounds
    except ValueError as e:
        raise ValueError("invalid bounds argument {!r}".format(bounds)) from e

    min_i = bisect.bisect_left(longitudes, west)
    max_i = bisect.bisect_right(longitudes, east)
    min_j = bisect.bisect_left(latitudes, south)
    max_j = bisect.bisect_right(latitudes, north)

    I = np.array(range(min_i, max_i))
    J = np.array(range(min_j, max_j))
    return cartesian2(I, J)

def mask_matrix(bounds, longitudes, latitudes):
    """Construct a sparse matrix which is 1 at all latitude+longitude
       grid points inside the rectangle BOUNDS, 0 outside.
       LATITUDES and LONGITUDES must be sorted.
    """
    I, J = mask_ij(bounds, longitudes, latitudes)

    return sparse.csr_matrix((np.ones_like(I), (I, J)),
                             shape=(len(longitudes), len(latitudes)))

class LocationRowOnDisk(tables.IsDescription):
    """The row format of the pytables table used to save Location objects
       on disk.  See Location.save and Location.load."""
    grid_x    = tables.UInt32Col()
    grid_y    = tables.UInt32Col()
    longitude = tables.Float64Col()
    latitude  = tables.Float64Col()
    prob_mass = tables.Float32Col()

class Location:
    """An estimated location for a host.  This is represented by a
    probability mass function over the surface of the Earth, quantized
    to a cell grid, and stored as a sparse matrix.

    Properties:
      resolution  - Grid resolution, in meters at the equator
      lon_spacing - East-west (longitude) grid resolution, in decimal degrees
      lat_spacing - North-south (latitude) grid resolution, in decimal degrees
      fuzz        - Coastline uncertainty factor, in meters at the equator
      north       - Northernmost latitude covered by the grid
      south       - Southernmost latitude ditto
      east        - Easternmost longitude ditto
      west        - Westernmost longitude ditto
      latitudes   - Vector of latitude values corresponding to grid points
      longitudes  - Vector of longitude values ditto
      probability - Probability mass matrix (may be lazily computed)
      bounds      - Bounding region of the nonzero portion of the
                    probability mass matrix (may be lazily computed)
      centroid    - Centroid of the nonzero &c
      area        - Weighted area of the nonzero &c
      covariance  - Covariance matrix of the nonzero &c
                    (relative to the centroid)
      rep_pt      - "Representative point" of the nonzero &c; see docstring
                    for exactly what this means
      annotations - Dictionary of arbitrary additional metadata; saved and
                    loaded but not otherwise inspected by this code

    You will normally not construct bare Location objects directly, only
    Map and Observation objects (these are subclasses).  However, any two
    Locations can be _intersected_ to produce a new one.

    A Location is _vacuous_ if it has no nonzero entries in its
    probability matrix.

    """
    def __init__(self, *,
                 resolution, fuzz, lon_spacing, lat_spacing,
                 north, south, east, west,
                 longitudes, latitudes,
                 probability=None, vacuity=None, bounds=None,
                 centroid=None, covariance=None, rep_pt=None,
                 loaded_from=None, annotations=None
    ):
        self.resolution   = resolution
        self.fuzz         = fuzz
        self.north        = north
        self.south        = south
        self.east         = east
        self.west         = west
        self.lon_spacing  = lon_spacing
        self.lat_spacing  = lat_spacing
        self.longitudes   = longitudes
        self.latitudes    = latitudes
        self._probability = probability
        self._vacuous     = vacuity
        self._bounds      = bounds
        self._centroid    = centroid
        self._covariance  = covariance
        self._rep_pt      = rep_pt
        self._loaded_from = loaded_from
        self._area        = None
        self.annotations  = annotations if annotations is not None else {}

    @property
    def probability(self):
        if self._probability is None:
            self.compute_probability_matrix_now()
        return self._probability

    @property
    def vacuous(self):
        if self._vacuous is None:
            self.compute_probability_matrix_now()
        return self._vacuous

    @property
    def centroid(self):
        if self._centroid is None:
            self.compute_centroid_now()
        return self._centroid

    @property
    def covariance(self):
        if self._covariance is None:
            self.compute_centroid_now()
        return self._covariance

    @property
    def area(self):
        """Weighted area of the nonzero region of the probability matrix."""

        if self._area is None:
            # Notionally, each grid point should be treated as a
            # rectangle of parallels and meridians _centered_ on the
            # point.  The area of such a rectangle, however, only
            # depends on its latitude and its breadth; the actual
            # longitude values don't matter.  Since the grid is
            # equally spaced, we can use [0],[1] always, and then we
            # do not have to worry about crossing the discontinuity at ±180.
            west = self.longitudes[0]
            east = self.longitudes[1]
            # For latitude, the actual values do matter, but the map
            # never goes all the way to the poles, and the grid is
            # equally spaced, so we can precompute the north-south
            # delta from any pair of latitudes and not have to worry
            # about running off the ends of the array.
            d_lat = (self.latitudes[1] - self.latitudes[0]) / 2

            # We don't need X, so throw it away immediately.  (We
            # don't use iter_csr_nonzero here because we need to
            # modify V.)
            X, Y, V = sparse.find(self.probability); X = None

            # The value vector is supposed to be normalized, but make
            # sure it is, and then adjust from 1-overall to 1-per-cell
            # normalization.
            assert len(V.shape) == 1
            S = V.sum()
            if S == 0:
                return 0
            if S != 1:
                V /= S
            V *= V.shape[0]

            area = 0
            for y, v in zip(Y, V):
                north = self.latitudes[y] + d_lat
                south = self.latitudes[y] - d_lat
                if not (-90 <= south < north <= 90):
                    raise AssertionError("expected -90 <= {} < {} <= 90"
                                         .format(south, north))

                tile = sh_transform(wgs_to_cea, Box(west, south, east, north))
                area += v * tile.area

            self._area = area
        return self._area

    @property
    def rep_pt(self, epsilon=1e-8):
        """Representative point of the nonzero region of the probability
           matrix.  This is, of all points with the greatest probability,
           the one closest to the centroid.
        """
        if self._rep_pt is None:
            lons = self.longitudes
            lats = self.latitudes
            cen = self.centroid
            aeqd_cen = pyproj.Proj(proj='aeqd', ellps='WGS84', datum='WGS84',
                                   lon_0=cen[0], lat_0=cen[1])
            wgs_to_aeqd = functools.partial(pyproj.transform,
                                            wgs_proj, aeqd_cen)
            # mathematically, wgs_to_aeqd(Point(lon, lat)) == Point(0, 0);
            # the latter is faster and more precise
            cen_pt = Point(0,0)

            # It is unacceptably costly to construct a shapely MultiPoint
            # out of some locations with large regions (can require more than
            # 32GB of scratch memory).  Instead, iterate over the points
            # one at a time.
            max_prob = 0
            min_dist = math.inf
            rep_pt = None
            for x, y, v in iter_csr_nonzero(self.probability):
                lon = lons[x]
                lat = lats[y]
                if rep_pt is None or v > max_prob - epsilon:
                    dist = WGS84dist(cen[0], cen[1], lon, lat)
                    # v < max_prob has already been excluded
                    if (rep_pt is None or v > max_prob or
                        (v > max_prob - epsilon and dist < min_dist)):
                        rep_pt = [lon, lat]
                        max_prob = max(max_prob, v)
                        min_dist = dist

            if rep_pt is None:
                rep_pt = cen
            else:
                rep_pt = np.array(rep_pt)
            self._rep_pt = rep_pt
        return self._rep_pt

    def distance_to_point(self, lon, lat):
        """Find the shortest geodesic distance from (lon, lat) to a nonzero
           cell of the probability matrix."""
        aeqd_pt = pyproj.Proj(proj='aeqd', ellps='WGS84', datum='WGS84',
                              lon_0=lon, lat_0=lat)
        wgs_to_aeqd = functools.partial(pyproj.transform, wgs_proj, aeqd_pt)
        # mathematically, wgs_to_aeqd(Point(lon, lat)) == Point(0, 0);
        # the latter is faster and more precise
        pt = Point(0, 0)

        # It is unacceptably costly to construct a shapely MultiPoint
        # out of some locations with large regions (requires more than
        # 32GB of scratch memory).  Instead, iterate over the points
        # one at a time.
        min_distance = math.inf
        for x, y, v in iter_csr_nonzero(self.probability):
            cell = sh_transform(wgs_to_aeqd, Point(self.longitudes[x],
                                                   self.latitudes[y]))
            if pt.distance(cell) - self.resolution*2 < min_distance:
                cell = cell.buffer(self.resolution * 3/2)
                min_distance = min(min_distance, pt.distance(cell))

        if min_distance < self.resolution * 3/2:
            return 0
        return min_distance

    def contains_point(self, lon, lat):
        """True if a grid cell with a nonzero probability contains
           or adjoins (lon, lat)."""
        i = bisect.bisect_left(self.longitudes, lon)
        j = bisect.bisect_left(self.latitudes, lat)
        return (self.probability[i-1, j-1] > 0 or
                self.probability[i,   j-1] > 0 or
                self.probability[i+1, j-1] > 0 or
                self.probability[i-1, j  ] > 0 or
                self.probability[i,   j  ] > 0 or
                self.probability[i+1, j  ] > 0 or
                self.probability[i-1, j+1] > 0 or
                self.probability[i,   j+1] > 0 or
                self.probability[i+1, j+1] > 0)

    def compute_probability_matrix_now(self):
        """Compute and set self._probability and self._vacuous.
        """
        if self._probability is not None:
            return

        if self._loaded_from:
            self._lazy_load_pmatrix()
        else:
            M, vac = self.compute_probability_matrix_within(self.bounds)
            self._probability = M
            self._vacuous = vac

    def compute_probability_matrix_within(self, bounds):
        """Subclasses must override if _probability is lazily computed.
           Returns a tuple (matrix, vacuous).
        """
        assert self._probability is not None
        assert self._vacuous is not None

        if self._vacuous:
            return self._probability, True # 0 everywhere, so 0 within bounds

        if bounds.is_empty or bounds.bounds == ():
            return (
                sparse.csr_matrix((len(self.longitudes),
                                   len(self.latitudes))),
                True
            )

        M = (mask_matrix(bounds.bounds, self.longitudes, self.latitudes)
             .multiply(self._probability))
        s = M.sum()
        if s:
            M /= s
            return M, False
        else:
            return M, True

    @property
    def bounds(self):
        if self._bounds is None:
            self.compute_bounding_region_now()
        return self._bounds

    def compute_bounding_region_now(self):
        """Subclasses must implement if necessary:
           compute and set self._bounds.
        """
        if self._bounds is None and self._loaded_from:
            self._lazy_load_pmatrix()
        assert self._bounds is not None

    def intersection(self, other, bounds=None):
        """Compute the intersection of this object's probability matrix with
           OTHER's.  If BOUNDS is specified, we don't care about
           anything outside that area, and it will become the bounding
           region of the result; otherwise this object and OTHER's
           bounding regions are intersected first and the computation
           is restricted to that region.
        """

        if (self.resolution  != other.resolution or
            self.fuzz        != other.fuzz or
            self.north       != other.north or
            self.south       != other.south or
            self.east        != other.east or
            self.west        != other.west or
            self.lon_spacing != other.lon_spacing or
            self.lat_spacing != other.lat_spacing):
            raise ValueError("can't intersect locations with "
                             "inconsistent grids")

        if bounds is None:
            bounds = self.bounds.intersection(other.bounds)

        # Compute P(self AND other), but only consider points inside
        # BOUNDS.  For simplicity we actually look at the quantized
        # bounding rectangle of BOUNDS.
        M1, V1 = self.compute_probability_matrix_within(bounds)
        M2, V2 = other.compute_probability_matrix_within(bounds)

        if V1:
            M = M1
            V = True
        elif V2:
            M = M2
            V = True
        else:
            M = None
            V = False
            # Optimization: if M1 and M2 have the same set of nonzero
            # entries, and all the nonzero values in one matrix are equal
            # or nearly so, then just use the other matrix as the result,
            # because the multiply-and-then-normalize operation will be a
            # nop.
            if (np.array_equal(M1.indptr, M2.indptr) and
                np.array_equal(M1.indices, M2.indices)):
                if np.allclose(M1.data, M1.data[0]):
                    M = M2
                elif np.allclose(M2.data, M2.data[0]):
                    M = M1

            if M is None:
                M = M1.multiply(M2)
                s = M.sum()
                if s:
                    M /= s
                else:
                    V = True
                M.eliminate_zeros()

        return Location(
            resolution  = self.resolution,
            fuzz        = self.fuzz,
            north       = self.north,
            south       = self.south,
            east        = self.east,
            west        = self.west,
            lon_spacing = self.lon_spacing,
            lat_spacing = self.lat_spacing,
            longitudes  = self.longitudes,
            latitudes   = self.latitudes,
            probability = M,
            vacuity     = V,
            bounds      = bounds
        )

    def compute_centroid_now(self):
        """Compute the weighted centroid and covariance matrix
           of the probability mass function.
        """
        if self._centroid is not None: return

        # The centroid of a cloud of points is just the average of
        # their coordinates, but this only works correctly in
        # geocentric Cartesian space, not in lat/long space.

        X = []
        Y = []
        Z = []
        for i, j, v in iter_csr_nonzero(self.probability):
            lon = self.longitudes[i]
            lat = self.latitudes[j]
            # PROJ.4 requires a dummy third argument when converting
            # to geocentric (this appears to be interpreted as meters
            # above/below the datum).
            x, y, z = wgs_to_gcen(lon, lat, 0)
            if math.isinf(x) or math.isinf(y) or math.isinf(z):
                sys.stderr.write("wgs_to_gcen({}, {}, 0) = {}, {}, {}\n"
                                 .format(lon, lat, x, y, z))
            else:
                X.append(x*v)
                Y.append(y*v)
                Z.append(z*v)

        # We leave the covariance matrix in geocentric terms, since
        # I'm not sure how to transform it back to lat/long space, or
        # if that even makes sense.
        M = np.vstack((X, Y, Z))
        self._covariance = np.cov(M)

        # Since the probability matrix is normalized, it is not
        # necessary to divide the weighted sums by anything to get
        # the means.
        lon, lat, _ = gcen_to_wgs(*np.sum(M, 1))
        if math.isinf(lat) or math.isinf(lon):
            raise ValueError("bogus centroid {}/{} - X={} Y={} Z={}"
                             .format(lat, lon, X, Y, Z))
        self._centroid = np.array((lon, lat))

    def save(self, fname):
        """Write out this location to an HDF file.
           For compactness, we write only the nonzero entries in a
           pytables record form, and we _don't_ write out the full
           longitude/latitude grid (it can be reconstructed from
           the other metadata).
        """
        self.compute_centroid_now()

        with tables.open_file(fname, mode="w", title="location") as f:
            t = f.create_table(f.root, "location",
                               LocationRowOnDisk, "location",
                               expectedrows=self.probability.getnnz())
            t.attrs.resolution  = self.resolution
            t.attrs.fuzz        = self.fuzz
            t.attrs.north       = self.north
            t.attrs.south       = self.south
            t.attrs.east        = self.east
            t.attrs.west        = self.west
            t.attrs.lon_spacing = self.lon_spacing
            t.attrs.lat_spacing = self.lat_spacing
            t.attrs.lon_count   = len(self.longitudes)
            t.attrs.lat_count   = len(self.latitudes)
            t.attrs.centroid    = self.centroid
            t.attrs.covariance  = self.covariance

            if self.annotations:
                t.attrs.annotations = self.annotations

            cur = t.row
            for i, j, pmass in iter_csr_nonzero(self.probability):
                lon = self.longitudes[i]
                lat = self.latitudes[j]
                cur['grid_x']    = i
                cur['grid_y']    = j
                cur['longitude'] = lon
                cur['latitude']  = lat
                cur['prob_mass'] = pmass
                cur.append()

            t.flush()

    def _lazy_load_pmatrix(self):
        assert self._loaded_from is not None
        with tables.open_file(self._loaded_from, "r") as f:
            t = f.root.location
            M = sparse.dok_matrix((t.attrs.lon_count, t.attrs.lat_count),
                                  dtype=np.float32)
            vacuous = True
            negative_warning = False
            for row in t.iterrows():
                pmass = row['prob_mass']
                # The occasional zero is normal, but negative numbers
                # should never occur.
                if pmass > 0:
                    M[row['grid_x'], row['grid_y']] = pmass
                    vacuous = False
                elif pmass < 0:
                    if not negative_warning:
                        sys.stderr.write(fname + ": warning: negative pmass\n")
                        negative_warning = True

            M = M.tocsr()

            if vacuous:
                wb = 0
                eb = 0
                sb = 0
                nb = 0
            else:
                i, j = M.nonzero()
                wb = self.longitudes[i.min()]
                eb = self.longitudes[i.max()]
                sb = self.latitudes[j.min()]
                nb = self.latitudes[j.max()]

            self._probability = M
            self._vacuous = vacuous
            self._bounds = Box(wb, sb, eb, nb)

    @classmethod
    def load(cls, fname):
        """Read an HDF file containing a location (the result of save())
           and instantiate a Location object from it.  The probability
           matrix is lazily loaded.
        """

        with tables.open_file(fname, "r") as f:
            t = f.root.location

            longs = np.linspace(t.attrs.west, t.attrs.east,
                                t.attrs.lon_count)
            lats = np.linspace(t.attrs.south, t.attrs.north,
                               t.attrs.lat_count)

            return cls(
                resolution  = t.attrs.resolution,
                fuzz        = t.attrs.fuzz,
                north       = t.attrs.north,
                south       = t.attrs.south,
                east        = t.attrs.east,
                west        = t.attrs.west,
                lon_spacing = t.attrs.lon_spacing,
                lat_spacing = t.attrs.lat_spacing,
                longitudes  = longs,
                latitudes   = lats,
                centroid    = getattr(t.attrs, 'centroid', None),
                covariance  = getattr(t.attrs, 'covariance', None),
                annotations = getattr(t.attrs, 'annotations', None),
                loaded_from = fname
            )

class Map(Location):
    """The map on which to locate a host.

       Maps are defined by HDF5 files (see maps/ for the program that
       generates these from shapefiles) that define a grid over the
       surface of the Earth and a "baseline matrix" which specifies
       the Bayesian prior probability of locating a host at any point
       on that grid.  (For instance, nobody puts servers in the middle
       of the ocean.)
    """

    def __init__(self, mapfile):
        with tables.open_file(mapfile, 'r') as f:
            M = f.root.baseline
            if M.shape[0] == len(M.attrs.longitudes):
                baseline = sparse.csr_matrix(M)
            elif M.shape[1] == len(M.attrs.longitudes):
                baseline = sparse.csr_matrix(M).T
            else:
                raise RuntimeError(
                    "mapfile matrix shape {!r} is inconsistent with "
                    "lon/lat vectors ({},{})"
                    .format(M.shape,
                            len(M.attrs.longitudes),
                            len(M.attrs.latitudes)))

            # The probabilities stored in the file are not normalized.
            s = baseline.sum()
            assert s > 0
            baseline /= s

            # Note: this bound may not be tight, but it should be
            # good enough.  It's not obvious to me how to extract
            # a tight bounding rectangle from a scipy sparse matrix.
            bounds      = Box(M.attrs.west, M.attrs.south,
                              M.attrs.east, M.attrs.north)
            if not bounds.is_valid:
                bounds = bounds.buffer(0)
                assert bounds.is_valid
            Location.__init__(
                self,
                resolution  = M.attrs.resolution,
                fuzz        = M.attrs.fuzz,
                north       = M.attrs.north,
                south       = M.attrs.south,
                east        = M.attrs.east,
                west        = M.attrs.west,
                lon_spacing = M.attrs.lon_spacing,
                lat_spacing = M.attrs.lat_spacing,
                longitudes  = M.attrs.longitudes,
                latitudes   = M.attrs.latitudes,
                probability = baseline,
                vacuity     = False,
                bounds      = bounds
            )

class Observation(Location):
    """A single observation of the distance to a host.

       An observation is defined by a map (used only for its grid;
       if you want to intersect the observation with the map, do that
       explicitly), the longitude and latitude of a reference point, a
       _ranging function_ (see ageo.ranging) that computes probability
       as a function of distance, calibration data for the ranging
       function (see ageo.calibration) and finally a set of observed
       round-trip times.

       Both the bounds and the probability matrix are computed lazily.

    """

    def __init__(self, *,
                 basemap, ref_lon, ref_lat,
                 range_fn, calibration, rtts):

        Location.__init__(
            self,
            resolution  = basemap.resolution,
            fuzz        = basemap.fuzz,
            north       = basemap.north,
            south       = basemap.south,
            east        = basemap.east,
            west        = basemap.west,
            lon_spacing = basemap.lon_spacing,
            lat_spacing = basemap.lat_spacing,
            longitudes  = basemap.longitudes,
            latitudes   = basemap.latitudes
        )
        self.ref_lon     = ref_lon
        self.ref_lat     = ref_lat
        self.calibration = calibration
        self.rtts        = rtts
        self.range_fn    = range_fn(calibration, rtts, basemap.fuzz)

    def compute_bounding_region_now(self):
        if self._bounds is not None: return

        distance_bound = self.range_fn.distance_bound()

        # If the distance bound is too close to half the circumference
        # of the Earth, the projection operation below will produce an
        # invalid polygon.  We don't get much use out of a bounding
        # region that includes the whole planet but for a tiny disk
        # (which will probably be somewhere in the ocean anyway) so
        # just give up and say that the bound is the entire planet.
        # Similarly, if the distance bound is zero, give up.
        if distance_bound > 19975000 or distance_bound == 0:
            self._bounds = Box(self.west, self.south, self.east, self.north)
            return

        # To find all points on the Earth within a certain distance of
        # a reference latitude and longitude, back-project onto the
        # Earth from an azimuthal-equidistant map with its zero point
        # at the reference latitude and longitude.
        aeqd = pyproj.Proj(proj='aeqd', ellps='WGS84', datum='WGS84',
                           lat_0=self.ref_lat, lon_0=self.ref_lon)

        try:
            disk = sh_transform(
                functools.partial(pyproj.transform, aeqd, wgs_proj),
                Disk(0, 0, distance_bound))

            # Two special cases must be manually dealt with.  First, if
            # any side of the "circle" (really a many-sided polygon)
            # crosses the coordinate singularity at longitude ±180, we
            # must replace it with a diversion to either the north or
            # south pole (whichever is closer) to ensure that it still
            # encloses all of the area it should.
            boundary = np.array(disk.boundary)
            i = 0
            while i < boundary.shape[0] - 1:
                if abs(boundary[i+1,0] - boundary[i,0]) > 180:
                    pole = self.south if boundary[i,1] < 0 else self.north
                    west = self.west if boundary[i,0] < 0 else self.east
                    east = self.east if boundary[i,0] < 0 else self.west

                    boundary = np.insert(boundary, i+1, [
                        [west, boundary[i,1]],
                        [west, pole],
                        [east, pole],
                        [east, boundary[i+1,1]]
                    ], axis=0)
                    i += 5
                else:
                    i += 1
            # If there were two edges that crossed the singularity and they
            # were both on the same side of the equator, the excursions will
            # coincide and shapely will be unhappy.  buffer(0) corrects this.
            disk = Polygon(boundary).buffer(0)

            # Second, if the disk is very large, the projected disk might
            # enclose the complement of the region that it ought to enclose.
            # If it doesn't contain the reference point, we must subtract it
            # from the entire map.
            origin = Point(self.ref_lon, self.ref_lat)
            if not disk.contains(origin):
                disk = (Box(self.west, self.south, self.east, self.north)
                        .difference(disk))

            assert disk.is_valid
            assert disk.contains(origin)
            self._bounds = disk
        except Exception as e:
            setattr(e, 'offending_disk', disk)
            setattr(e, 'offending_obs', self)
            raise

    def compute_probability_matrix_within(self, bounds):
        if not bounds.is_empty and bounds.bounds != ():

            I, J = mask_ij(bounds.intersection(self.bounds).bounds,
                           self.longitudes,
                           self.latitudes)

            pvals = self.range_fn.unnormalized_pvals(
                WGS84dist(self.ref_lon,
                          self.ref_lat,
                          self.longitudes[I],
                          self.latitudes[J]))

            s = pvals.sum()
            if s:
                pvals /= s
                M = sparse.csr_matrix((pvals, (I, J)),
                                      shape=(len(self.longitudes),
                                             len(self.latitudes)))
                M.eliminate_zeros()
                return (M, False)

        return (
            sparse.csr_matrix((len(self.longitudes),
                               len(self.latitudes))),
            True
        )

