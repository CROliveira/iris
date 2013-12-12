# (C) British Crown Copyright 2010 - 2013, Met Office
#
# This file is part of Iris.
#
# Iris is free software: you can redistribute it and/or modify it under
# the terms of the GNU Lesser General Public License as published by the
# Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# Iris is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Lesser General Public License for more details.
#
# You should have received a copy of the GNU Lesser General Public License
# along with Iris.  If not, see <http://www.gnu.org/licenses/>.
"""
A package for handling multi-dimensional data and associated metadata.

.. note ::

    The Iris documentation has further usage information, including
    a :ref:`user guide <user_guide_index>` which should be the first port of
    call for new users.

The functions in this module provide the main way to load and/or save
your data.

The :func:`load` function provides a simple way to explore data from
the interactive Python prompt. It will convert the source data into
:class:`Cubes <iris.cube.Cube>`, and combine those cubes into
higher-dimensional cubes where possible.

The :func:`load_cube` and :func:`load_cubes` functions are similar to
:func:`load`, but they raise an exception if the number of cubes is not
what was expected. They are more useful in scripts, where they can
provide an early sanity check on incoming data.

The :func:`load_raw` function is provided for those occasions where the
automatic combination of cubes into higher-dimensional cubes is
undesirable. However, it is intended as a tool of last resort! If you
experience a problem with the automatic combination process then please
raise an issue with the Iris developers.

To persist a cube to the file-system, use the :func:`save` function.

All the load functions share very similar arguments:

    * uris:
        Either a single filename/URI expressed as a string, or an
        iterable of filenames/URIs.

        Filenames can contain `~` or `~user` abbreviations, and/or
        Unix shell-style wildcards (e.g. `*` and `?`). See the
        standard library function :func:`os.path.expanduser` and
        module :mod:`fnmatch` for more details.

    * constraints:
        Either a single constraint, or an iterable of constraints.
        Each constraint can be either a CF standard name, an instance of
        :class:`iris.Constraint`, or an instance of
        :class:`iris.AttributeConstraint`.

        For example::

            # Load air temperature data.
            load_cube(uri, 'air_temperature')

            # Load data with a specific model level number.
            load_cube(uri, iris.Constraint(model_level_number=1))

            # Load data with a specific STASH code.
            load_cube(uri, iris.AttributeConstraint(STASH='m01s00i004'))

    * callback:
        A function to add metadata from the originating field and/or URI
        which obeys the following rules:

            1. Function signature must be: ``(cube, field, filename)``
            2. Must not return any value - any alterations to the cube
               must be made by reference
            3. If the cube is to be rejected the callback must raise an
               :class:`iris.exceptions.IgnoreCubeException`

        For example::

            def callback(cube, field, filename):
                # Extract ID from filenames given as: <prefix>__<exp_id>
                experiment_id = filename.split('__')[1]
                experiment_coord = iris.coords.AuxCoord(
                    experiment_id, long_name='experiment_id')
                cube.add_aux_coord(experiment_coord)

Format-specific translation behaviour can be modified by using:
    :func:`iris.fileformats.pp.add_load_rules`

    :func:`iris.fileformats.grib.add_load_rules`

"""
import contextlib
import itertools
import logging
import os
import threading
import warnings

import iris.config
import iris.cube
import iris._constraints
import iris.fileformats
import iris.io


# Iris revision.
__version__ = '1.6.0-dev'

# Restrict the names imported when using "from iris import *"
__all__ = ['load', 'load_cube', 'load_cubes', 'load_raw',
           'save', 'Constraint', 'AttributeConstraint', 'TimeConstraint',
           'sample_data_path', 'site_configuration', 'FUTURE']


# When required, log the usage of Iris.
if iris.config.IMPORT_LOGGER:
    logging.getLogger(iris.config.IMPORT_LOGGER).info('iris %s' % __version__)


Constraint = iris._constraints.Constraint
AttributeConstraint = iris._constraints.AttributeConstraint
TimeConstraint = iris._constraints.TimeConstraint


class Future(threading.local):
    """
    Run-time configuration options.

    To adjust the values simply update the relevant attribute from
    within your code. For example::

        iris.FUTURE.cell_time_objects = True

    If Iris code is executed with multiple threads, note the values of
    these options are thread-specific.

    Currently, the only option available is `cell_time_objects` which
    controls whether the :meth:`iris.coords.Coord.cell()` method returns
    time coordinate values as simple numbers or as time objects with
    attributes for year, month, day, etc. In particular, this allows one
    to express certain time constraints using a simpler, more
    transparent syntax, such as::

        # To select all data defined at midday.
        Constraint(time=lambda cell: cell.point.hour == 12)

        # To ignore the 29th of February.
        Constraint(time=lambda cell: cell.point.day != 29 and
                                     cell.point.month != 2)

    """

    def __init__(self, cell_time_objects=False):
        """This one's for the init"""
        self.__dict__['cell_time_objects'] = cell_time_objects

    def __repr__(self):
        return 'Future(cell_time_objects={})'.format(self.cell_time_objects)

    def __setattr__(self, name, value):
        if name not in self.__dict__:
            msg = "'Future' object has no attribute {!r}".format(name)
            raise AttributeError(msg)
        self.__dict__[name] = value

    @contextlib.contextmanager
    def context(self, **kwargs):
        """
        Return a context manager which allows temporary modification of
        the option values for the active thread.

        On entry to the `with` statement, all keyword arguments are
        applied to the Future object. On exit from the `with`
        statement, the previous state is restored.

        For example::

            with iris.FUTURE.context():
                iris.FUTURE.cell_time_objects = True
                # ... code which expects time objects

        Or more concisely::

            with iris.FUTURE.context(cell_time_objects=True):
                # ... code which expects time objects

        """
        # Save the current context
        current_state = self.__dict__.copy()
        # Update the state
        for name, value in kwargs.iteritems():
            setattr(self, name, value)
        yield
        # Return the state
        self.__dict__.clear()
        self.__dict__.update(current_state)


FUTURE = Future()


# Initialise the site configuration dictionary.
#: Iris site configuration dictionary.
site_configuration = {}

try:
    from iris.site_config import update as _update
except ImportError:
    pass
else:
    _update(site_configuration)


def _generate_cubes(uris, callback):
    """Returns a generator of cubes given the URIs and a callback."""
    if isinstance(uris, basestring):
        uris = [uris]

    # Group collections of uris by their iris handler
    # Create list of tuples relating schemes to part names
    uri_tuples = sorted(iris.io.decode_uri(uri) for uri in uris)

    for scheme, groups in (itertools.groupby(uri_tuples, key=lambda x: x[0])):
        # Call each scheme handler with the appropriate URIs
        if scheme == 'file':
            part_names = [x[1] for x in groups]
            for cube in iris.io.load_files(part_names, callback):
                yield cube
        elif scheme in ['http', 'https']:
            urls = [':'.join(x) for x in groups]
            for cube in iris.io.load_http(urls, callback):
                yield cube
        else:
            raise ValueError('Iris cannot handle the URI scheme: %s' % scheme)


def _load_collection(uris, constraints=None, callback=None):
    try:
        cubes = _generate_cubes(uris, callback)
        result = iris.cube._CubeFilterCollection.from_cubes(cubes, constraints)
    except EOFError as e:
        raise iris.exceptions.TranslationError(
            "The file appears empty or incomplete: {!r}".format(e.message))
    return result


def load(uris, constraints=None, callback=None):
    """
    Loads any number of Cubes for each constraint.

    For a full description of the arguments, please see the module
    documentation for :mod:`iris`.

    Args:

    * uris:
        One or more filenames/URIs.

    Kwargs:

    * constraints:
        One or more constraints.
    * callback:
        A modifier/filter function.

    Returns:
        An :class:`iris.cube.CubeList`.

    """
    return _load_collection(uris, constraints, callback).merged().cubes()


def load_cube(uris, constraint=None, callback=None):
    """
    Loads a single cube.

    For a full description of the arguments, please see the module
    documentation for :mod:`iris`.

    Args:

    * uris:
        One or more filenames/URIs.

    Kwargs:

    * constraints:
        A constraint.
    * callback:
        A modifier/filter function.

    Returns:
        An :class:`iris.cube.Cube`.

    """
    constraints = iris._constraints.list_of_constraints(constraint)
    if len(constraints) != 1:
        raise ValueError('only a single constraint is allowed')

    cubes = _load_collection(uris, constraints, callback).merged().cubes()

    if len(cubes) != 1:
        msg = 'Expected exactly one cube, found {}.'.format(len(cubes))
        raise iris.exceptions.ConstraintMismatchError(msg)
    return cubes[0]


def load_cubes(uris, constraints=None, callback=None):
    """
    Loads exactly one Cube for each constraint.

    For a full description of the arguments, please see the module
    documentation for :mod:`iris`.

    Args:

    * uris:
        One or more filenames/URIs.

    Kwargs:

    * constraints:
        One or more constraints.
    * callback:
        A modifier/filter function.

    Returns:
        An :class:`iris.cube.CubeList`.

    """
    # Merge the incoming cubes
    collection = _load_collection(uris, constraints, callback).merged()

    # Make sure we have exactly one merged cube per constraint
    bad_pairs = filter(lambda pair: len(pair) != 1, collection.pairs)
    if bad_pairs:
        fmt = '   {} -> {} cubes'
        bits = [fmt.format(pair.constraint, len(pair)) for pair in bad_pairs]
        msg = '\n' + '\n'.join(bits)
        raise iris.exceptions.ConstraintMismatchError(msg)

    return collection.cubes()


def load_raw(uris, constraints=None, callback=None):
    """
    Loads non-merged cubes.

    This function is provided for those occasions where the automatic
    combination of cubes into higher-dimensional cubes is undesirable.
    However, it is intended as a tool of last resort! If you experience
    a problem with the automatic combination process then please raise
    an issue with the Iris developers.

    For a full description of the arguments, please see the module
    documentation for :mod:`iris`.

    Args:

    * uris:
        One or more filenames/URIs.

    Kwargs:

    * constraints:
        One or more constraints.
    * callback:
        A modifier/filter function.

    Returns:
        An :class:`iris.cube.CubeList`.

    """
    return _load_collection(uris, constraints, callback).cubes()


save = iris.io.save


def sample_data_path(*path_to_join):
    """Given the sample data resource, returns the full path to the file."""
    return os.path.join(iris.config.SAMPLE_DATA_DIR, *path_to_join)
