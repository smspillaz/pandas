"""
Msgpack serializer support for reading and writing pandas data structures
to disk

portions of msgpack_numpy package, by Lev Givon were incorporated
into this module (and tests_packers.py)

License
=======

Copyright (c) 2013, Lev Givon.
All rights reserved.

Redistribution and use in source and binary forms, with or without
modification, are permitted provided that the following conditions are
met:

* Redistributions of source code must retain the above copyright
  notice, this list of conditions and the following disclaimer.
* Redistributions in binary form must reproduce the above
  copyright notice, this list of conditions and the following
  disclaimer in the documentation and/or other materials provided
  with the distribution.
* Neither the name of Lev Givon nor the names of any
  contributors may be used to endorse or promote products derived
  from this software without specific prior written permission.

THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS
"AS IS" AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT
LIMITED TO, THE IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR
A PARTICULAR PURPOSE ARE DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT
OWNER OR CONTRIBUTORS BE LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL,
SPECIAL, EXEMPLARY, OR CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT
LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR SERVICES; LOSS OF USE,
DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER CAUSED AND ON ANY
THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY, OR TORT
(INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE
OF THIS SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.
"""

from datetime import date, datetime, timedelta
from io import BytesIO
import os
import warnings

from dateutil.parser import parse
import numpy as np

from pandas.compat._optional import import_optional_dependency
from pandas.errors import PerformanceWarning
from pandas.util._move import (
    BadMove as _BadMove, move_into_mutable_buffer as _move_into_mutable_buffer)

from pandas.core.dtypes.common import (
    is_categorical_dtype, is_datetime64tz_dtype, is_object_dtype,
    needs_i8_conversion, pandas_dtype)

from pandas import (  # noqa:F401
    Categorical, CategoricalIndex, DataFrame, DatetimeIndex, Float64Index,
    Index, Int64Index, Interval, IntervalIndex, MultiIndex, NaT, Panel, Period,
    PeriodIndex, RangeIndex, Series, TimedeltaIndex, Timestamp)
from pandas.core import internals
from pandas.core.arrays import DatetimeArray, IntervalArray, PeriodArray
from pandas.core.arrays.sparse import BlockIndex, IntIndex
from pandas.core.generic import NDFrame
from pandas.core.internals import BlockManager, _safe_reshape, make_block
from pandas.core.sparse.api import SparseDataFrame, SparseSeries

from pandas.io.common import _stringify_path, get_filepath_or_buffer
from pandas.io.msgpack import ExtType, Packer as _Packer, Unpacker as _Unpacker

# until we can pass this into our conversion functions,
# this is pretty hacky
compressor = None


def to_msgpack(path_or_buf, *args, **kwargs):
    """
    msgpack (serialize) object to input file path

    .. deprecated:: 0.25.0

    to_msgpack is deprecated and will be removed in a future version.
    It is recommended to use pyarrow for on-the-wire transmission of
    pandas objects.

    Parameters
    ----------
    path_or_buf : string File path, buffer-like, or None
                  if None, return generated string
    args : an object or objects to serialize
    encoding : encoding for unicode objects
    append : boolean whether to append to an existing msgpack
             (default is False)
    compress : type of compressor (zlib or blosc), default to None (no
               compression)
    """
    warnings.warn("to_msgpack is deprecated and will be removed in a "
                  "future version.\n"
                  "It is recommended to use pyarrow for on-the-wire "
                  "transmission of pandas objects.",
                  FutureWarning, stacklevel=3)

    global compressor
    compressor = kwargs.pop('compress', None)
    append = kwargs.pop('append', None)
    if append:
        mode = 'a+b'
    else:
        mode = 'wb'

    def writer(fh):
        for a in args:
            fh.write(pack(a, **kwargs))

    path_or_buf = _stringify_path(path_or_buf)
    if isinstance(path_or_buf, str):
        with open(path_or_buf, mode) as fh:
            writer(fh)
    elif path_or_buf is None:
        buf = BytesIO()
        writer(buf)
        return buf.getvalue()
    else:
        writer(path_or_buf)


def read_msgpack(path_or_buf, encoding='utf-8', iterator=False, **kwargs):
    """
    Load msgpack pandas object from the specified
    file path

    .. deprecated:: 0.25.0

    read_msgpack is deprecated and will be removed in a future version.
    It is recommended to use pyarrow for on-the-wire transmission of
    pandas objects.

    Parameters
    ----------
    path_or_buf : string File path, BytesIO like or string
    encoding : Encoding for decoding msgpack str type
    iterator : boolean, if True, return an iterator to the unpacker
               (default is False)

    Returns
    -------
    obj : same type as object stored in file

    Notes
    -----
    read_msgpack is only guaranteed to be backwards compatible to pandas
    0.20.3.
    """
    warnings.warn("The read_msgpack is deprecated and will be removed in a "
                  "future version.\n"
                  "It is recommended to use pyarrow for on-the-wire "
                  "transmission of pandas objects.",
                  FutureWarning, stacklevel=3)

    path_or_buf, _, _, should_close = get_filepath_or_buffer(path_or_buf)
    if iterator:
        return Iterator(path_or_buf)

    def read(fh):
        unpacked_obj = list(unpack(fh, encoding=encoding, **kwargs))
        if len(unpacked_obj) == 1:
            return unpacked_obj[0]

        if should_close:
            try:
                path_or_buf.close()
            except IOError:
                pass
        return unpacked_obj

    # see if we have an actual file
    if isinstance(path_or_buf, str):
        try:
            exists = os.path.exists(path_or_buf)
        except (TypeError, ValueError):
            exists = False

        if exists:
            with open(path_or_buf, 'rb') as fh:
                return read(fh)
        else:
            return FileNotFoundError('{} not found'.format(path_or_buf))

    if isinstance(path_or_buf, bytes):
        # treat as a binary-like
        fh = None
        try:
            fh = BytesIO(path_or_buf)
            return read(fh)
        finally:
            if fh is not None:
                fh.close()
    elif hasattr(path_or_buf, 'read') and callable(path_or_buf.read):
        # treat as a buffer like
        return read(path_or_buf)

    raise ValueError('path_or_buf needs to be a string file path or file-like')


dtype_dict = {21: np.dtype('M8[ns]'),
              'datetime64[ns]': np.dtype('M8[ns]'),
              'datetime64[us]': np.dtype('M8[us]'),
              22: np.dtype('m8[ns]'),
              'timedelta64[ns]': np.dtype('m8[ns]'),
              'timedelta64[us]': np.dtype('m8[us]'),

              # this is platform int, which we need to remap to np.int64
              # for compat on windows platforms
              7: np.dtype('int64'),
              'category': 'category'
              }


def dtype_for(t):
    """ return my dtype mapping, whether number or name """
    if t in dtype_dict:
        return dtype_dict[t]
    return np.typeDict.get(t, t)


c2f_dict = {'complex': np.float64,
            'complex128': np.float64,
            'complex64': np.float32}

# windows (32 bit) compat
if hasattr(np, 'float128'):
    c2f_dict['complex256'] = np.float128


def c2f(r, i, ctype_name):
    """
    Convert strings to complex number instance with specified numpy type.
    """

    ftype = c2f_dict[ctype_name]
    return np.typeDict[ctype_name](ftype(r) + 1j * ftype(i))


def convert(values):
    """ convert the numpy values to a list """

    dtype = values.dtype

    if is_categorical_dtype(values):
        return values

    elif is_object_dtype(dtype):
        return values.ravel().tolist()

    if needs_i8_conversion(dtype):
        values = values.view('i8')
    v = values.ravel()

    if compressor == 'zlib':
        zlib = import_optional_dependency(
            "zlib",
            extra="zlib is required when `compress='zlib'`."
        )

        # return string arrays like they are
        if dtype == np.object_:
            return v.tolist()

        # convert to a bytes array
        v = v.tostring()
        return ExtType(0, zlib.compress(v))

    elif compressor == 'blosc':
        blosc = import_optional_dependency(
            "blosc",
            extra="zlib is required when `compress='blosc'`."
        )

        # return string arrays like they are
        if dtype == np.object_:
            return v.tolist()

        # convert to a bytes array
        v = v.tostring()
        return ExtType(0, blosc.compress(v, typesize=dtype.itemsize))

    # ndarray (on original dtype)
    return ExtType(0, v.tostring())


def unconvert(values, dtype, compress=None):

    as_is_ext = isinstance(values, ExtType) and values.code == 0

    if as_is_ext:
        values = values.data

    if is_categorical_dtype(dtype):
        return values

    elif is_object_dtype(dtype):
        return np.array(values, dtype=object)

    dtype = pandas_dtype(dtype).base

    if not as_is_ext:
        values = values.encode('latin1')

    if compress:
        if compress == 'zlib':
            zlib = import_optional_dependency(
                "zlib",
                extra="zlib is required when `compress='zlib'`."
            )
            decompress = zlib.decompress
        elif compress == 'blosc':
            blosc = import_optional_dependency(
                "blosc",
                extra="zlib is required when `compress='blosc'`."
            )
            decompress = blosc.decompress
        else:
            raise ValueError("compress must be one of 'zlib' or 'blosc'")

        try:
            return np.frombuffer(
                _move_into_mutable_buffer(decompress(values)),
                dtype=dtype,
            )
        except _BadMove as e:
            # Pull the decompressed data off of the `_BadMove` exception.
            # We don't just store this in the locals because we want to
            # minimize the risk of giving users access to a `bytes` object
            # whose data is also given to a mutable buffer.
            values = e.args[0]
            if len(values) > 1:
                # The empty string and single characters are memoized in many
                # string creating functions in the capi. This case should not
                # warn even though we need to make a copy because we are only
                # copying at most 1 byte.
                warnings.warn(
                    'copying data after decompressing; this may mean that'
                    ' decompress is caching its result',
                    PerformanceWarning,
                )
                # fall through to copying `np.fromstring`

    # Copy the bytes into a numpy array.
    buf = np.frombuffer(values, dtype=dtype)
    buf = buf.copy()  # required to not mutate the original data
    buf.flags.writeable = True
    return buf


def encode(obj):
    """
    Data encoder
    """
    tobj = type(obj)
    if isinstance(obj, Index):
        if isinstance(obj, RangeIndex):
            return {'typ': 'range_index',
                    'klass': obj.__class__.__name__,
                    'name': getattr(obj, 'name', None),
                    'start': obj._range.start,
                    'stop': obj._range.stop,
                    'step': obj._range.step,
                    }
        elif isinstance(obj, PeriodIndex):
            return {'typ': 'period_index',
                    'klass': obj.__class__.__name__,
                    'name': getattr(obj, 'name', None),
                    'freq': getattr(obj, 'freqstr', None),
                    'dtype': obj.dtype.name,
                    'data': convert(obj.asi8),
                    'compress': compressor}
        elif isinstance(obj, DatetimeIndex):
            tz = getattr(obj, 'tz', None)

            # store tz info and data as UTC
            if tz is not None:
                tz = tz.zone
                obj = obj.tz_convert('UTC')
            return {'typ': 'datetime_index',
                    'klass': obj.__class__.__name__,
                    'name': getattr(obj, 'name', None),
                    'dtype': obj.dtype.name,
                    'data': convert(obj.asi8),
                    'freq': getattr(obj, 'freqstr', None),
                    'tz': tz,
                    'compress': compressor}
        elif isinstance(obj, (IntervalIndex, IntervalArray)):
            if isinstance(obj, IntervalIndex):
                typ = 'interval_index'
            else:
                typ = 'interval_array'
            return {'typ': typ,
                    'klass': obj.__class__.__name__,
                    'name': getattr(obj, 'name', None),
                    'left': getattr(obj, 'left', None),
                    'right': getattr(obj, 'right', None),
                    'closed': getattr(obj, 'closed', None)}
        elif isinstance(obj, MultiIndex):
            return {'typ': 'multi_index',
                    'klass': obj.__class__.__name__,
                    'names': getattr(obj, 'names', None),
                    'dtype': obj.dtype.name,
                    'data': convert(obj.values),
                    'compress': compressor}
        else:
            return {'typ': 'index',
                    'klass': obj.__class__.__name__,
                    'name': getattr(obj, 'name', None),
                    'dtype': obj.dtype.name,
                    'data': convert(obj.values),
                    'compress': compressor}

    elif isinstance(obj, Categorical):
        return {'typ': 'category',
                'klass': obj.__class__.__name__,
                'name': getattr(obj, 'name', None),
                'codes': obj.codes,
                'categories': obj.categories,
                'ordered': obj.ordered,
                'compress': compressor}

    elif isinstance(obj, Series):
        if isinstance(obj, SparseSeries):
            raise NotImplementedError(
                'msgpack sparse series is not implemented'
            )
            # d = {'typ': 'sparse_series',
            #     'klass': obj.__class__.__name__,
            #     'dtype': obj.dtype.name,
            #     'index': obj.index,
            #     'sp_index': obj.sp_index,
            #     'sp_values': convert(obj.sp_values),
            #     'compress': compressor}
            # for f in ['name', 'fill_value', 'kind']:
            #    d[f] = getattr(obj, f, None)
            # return d
        else:
            return {'typ': 'series',
                    'klass': obj.__class__.__name__,
                    'name': getattr(obj, 'name', None),
                    'index': obj.index,
                    'dtype': obj.dtype.name,
                    'data': convert(obj.values),
                    'compress': compressor}
    elif issubclass(tobj, NDFrame):
        if isinstance(obj, SparseDataFrame):
            raise NotImplementedError(
                'msgpack sparse frame is not implemented'
            )
            # d = {'typ': 'sparse_dataframe',
            #     'klass': obj.__class__.__name__,
            #     'columns': obj.columns}
            # for f in ['default_fill_value', 'default_kind']:
            #    d[f] = getattr(obj, f, None)
            # d['data'] = dict([(name, ss)
            #                 for name, ss in obj.items()])
            # return d
        else:

            data = obj._data
            if not data.is_consolidated():
                data = data.consolidate()

            # the block manager
            return {'typ': 'block_manager',
                    'klass': obj.__class__.__name__,
                    'axes': data.axes,
                    'blocks': [{'locs': b.mgr_locs.as_array,
                                'values': convert(b.values),
                                'shape': b.values.shape,
                                'dtype': b.dtype.name,
                                'klass': b.__class__.__name__,
                                'compress': compressor} for b in data.blocks]
                    }

    elif isinstance(obj, (datetime, date, np.datetime64, timedelta,
                          np.timedelta64)) or obj is NaT:
        if isinstance(obj, Timestamp):
            tz = obj.tzinfo
            if tz is not None:
                tz = tz.zone
            freq = obj.freq
            if freq is not None:
                freq = freq.freqstr
            return {'typ': 'timestamp',
                    'value': obj.value,
                    'freq': freq,
                    'tz': tz}
        if obj is NaT:
            return {'typ': 'nat'}
        elif isinstance(obj, np.timedelta64):
            return {'typ': 'timedelta64',
                    'data': obj.view('i8')}
        elif isinstance(obj, timedelta):
            return {'typ': 'timedelta',
                    'data': (obj.days, obj.seconds, obj.microseconds)}
        elif isinstance(obj, np.datetime64):
            return {'typ': 'datetime64',
                    'data': str(obj)}
        elif isinstance(obj, datetime):
            return {'typ': 'datetime',
                    'data': obj.isoformat()}
        elif isinstance(obj, date):
            return {'typ': 'date',
                    'data': obj.isoformat()}
        raise Exception(
            "cannot encode this datetimelike object: {obj}".format(obj=obj))
    elif isinstance(obj, Period):
        return {'typ': 'period',
                'ordinal': obj.ordinal,
                'freq': obj.freqstr}
    elif isinstance(obj, Interval):
        return {'typ': 'interval',
                'left': obj.left,
                'right': obj.right,
                'closed': obj.closed}
    elif isinstance(obj, BlockIndex):
        return {'typ': 'block_index',
                'klass': obj.__class__.__name__,
                'blocs': obj.blocs,
                'blengths': obj.blengths,
                'length': obj.length}
    elif isinstance(obj, IntIndex):
        return {'typ': 'int_index',
                'klass': obj.__class__.__name__,
                'indices': obj.indices,
                'length': obj.length}
    elif isinstance(obj, np.ndarray):
        return {'typ': 'ndarray',
                'shape': obj.shape,
                'ndim': obj.ndim,
                'dtype': obj.dtype.name,
                'data': convert(obj),
                'compress': compressor}
    elif isinstance(obj, np.number):
        if np.iscomplexobj(obj):
            return {'typ': 'np_scalar',
                    'sub_typ': 'np_complex',
                    'dtype': obj.dtype.name,
                    'real': np.real(obj).__repr__(),
                    'imag': np.imag(obj).__repr__()}
        else:
            return {'typ': 'np_scalar',
                    'dtype': obj.dtype.name,
                    'data': obj.__repr__()}
    elif isinstance(obj, complex):
        return {'typ': 'np_complex',
                'real': np.real(obj).__repr__(),
                'imag': np.imag(obj).__repr__()}

    return obj


def decode(obj):
    """
    Decoder for deserializing numpy data types.
    """

    typ = obj.get('typ')
    if typ is None:
        return obj
    elif typ == 'timestamp':
        freq = obj['freq'] if 'freq' in obj else obj['offset']
        return Timestamp(obj['value'], tz=obj['tz'], freq=freq)
    elif typ == 'nat':
        return NaT
    elif typ == 'period':
        return Period(ordinal=obj['ordinal'], freq=obj['freq'])
    elif typ == 'index':
        dtype = dtype_for(obj['dtype'])
        data = unconvert(obj['data'], dtype,
                         obj.get('compress'))
        return Index(data, dtype=dtype, name=obj['name'])
    elif typ == 'range_index':
        return RangeIndex(obj['start'],
                          obj['stop'],
                          obj['step'],
                          name=obj['name'])
    elif typ == 'multi_index':
        dtype = dtype_for(obj['dtype'])
        data = unconvert(obj['data'], dtype,
                         obj.get('compress'))
        data = [tuple(x) for x in data]
        return MultiIndex.from_tuples(data, names=obj['names'])
    elif typ == 'period_index':
        data = unconvert(obj['data'], np.int64, obj.get('compress'))
        d = dict(name=obj['name'], freq=obj['freq'])
        freq = d.pop('freq', None)
        return PeriodIndex(PeriodArray(data, freq), **d)

    elif typ == 'datetime_index':
        data = unconvert(obj['data'], np.int64, obj.get('compress'))
        d = dict(name=obj['name'], freq=obj['freq'])
        result = DatetimeIndex(data, **d)
        tz = obj['tz']

        # reverse tz conversion
        if tz is not None:
            result = result.tz_localize('UTC').tz_convert(tz)
        return result

    elif typ in ('interval_index', 'interval_array'):
        return globals()[obj['klass']].from_arrays(obj['left'],
                                                   obj['right'],
                                                   obj['closed'],
                                                   name=obj['name'])
    elif typ == 'category':
        from_codes = globals()[obj['klass']].from_codes
        return from_codes(codes=obj['codes'],
                          categories=obj['categories'],
                          ordered=obj['ordered'])

    elif typ == 'interval':
        return Interval(obj['left'], obj['right'], obj['closed'])
    elif typ == 'series':
        dtype = dtype_for(obj['dtype'])
        pd_dtype = pandas_dtype(dtype)

        index = obj['index']
        result = Series(unconvert(obj['data'], dtype, obj['compress']),
                        index=index,
                        dtype=pd_dtype,
                        name=obj['name'])
        return result

    elif typ == 'block_manager':
        axes = obj['axes']

        def create_block(b):
            values = _safe_reshape(unconvert(
                b['values'], dtype_for(b['dtype']),
                b['compress']), b['shape'])

            # locs handles duplicate column names, and should be used instead
            # of items; see GH 9618
            if 'locs' in b:
                placement = b['locs']
            else:
                placement = axes[0].get_indexer(b['items'])

            if is_datetime64tz_dtype(b['dtype']):
                assert isinstance(values, np.ndarray), type(values)
                assert values.dtype == 'M8[ns]', values.dtype
                values = DatetimeArray(values, dtype=b['dtype'])

            return make_block(values=values,
                              klass=getattr(internals, b['klass']),
                              placement=placement,
                              dtype=b['dtype'])

        blocks = [create_block(b) for b in obj['blocks']]
        return globals()[obj['klass']](BlockManager(blocks, axes))
    elif typ == 'datetime':
        return parse(obj['data'])
    elif typ == 'datetime64':
        return np.datetime64(parse(obj['data']))
    elif typ == 'date':
        return parse(obj['data']).date()
    elif typ == 'timedelta':
        return timedelta(*obj['data'])
    elif typ == 'timedelta64':
        return np.timedelta64(int(obj['data']))
    # elif typ == 'sparse_series':
    #    dtype = dtype_for(obj['dtype'])
    #    return SparseSeries(
    #        unconvert(obj['sp_values'], dtype, obj['compress']),
    #        sparse_index=obj['sp_index'], index=obj['index'],
    #        fill_value=obj['fill_value'], kind=obj['kind'], name=obj['name'])
    # elif typ == 'sparse_dataframe':
    #    return SparseDataFrame(
    #        obj['data'], columns=obj['columns'],
    #        default_fill_value=obj['default_fill_value'],
    #        default_kind=obj['default_kind']
    #    )
    # elif typ == 'sparse_panel':
    #    return SparsePanel(
    #        obj['data'], items=obj['items'],
    #        default_fill_value=obj['default_fill_value'],
    #        default_kind=obj['default_kind'])
    elif typ == 'block_index':
        return globals()[obj['klass']](obj['length'], obj['blocs'],
                                       obj['blengths'])
    elif typ == 'int_index':
        return globals()[obj['klass']](obj['length'], obj['indices'])
    elif typ == 'ndarray':
        return unconvert(obj['data'], np.typeDict[obj['dtype']],
                         obj.get('compress')).reshape(obj['shape'])
    elif typ == 'np_scalar':
        if obj.get('sub_typ') == 'np_complex':
            return c2f(obj['real'], obj['imag'], obj['dtype'])
        else:
            dtype = dtype_for(obj['dtype'])
            try:
                return dtype(obj['data'])
            except (ValueError, TypeError):
                return dtype.type(obj['data'])
    elif typ == 'np_complex':
        return complex(obj['real'] + '+' + obj['imag'] + 'j')
    elif isinstance(obj, (dict, list, set)):
        return obj
    else:
        return obj


def pack(o, default=encode,
         encoding='utf-8', unicode_errors='strict', use_single_float=False,
         autoreset=1, use_bin_type=1):
    """
    Pack an object and return the packed bytes.
    """

    return Packer(default=default, encoding=encoding,
                  unicode_errors=unicode_errors,
                  use_single_float=use_single_float,
                  autoreset=autoreset,
                  use_bin_type=use_bin_type).pack(o)


def unpack(packed, object_hook=decode,
           list_hook=None, use_list=False, encoding='utf-8',
           unicode_errors='strict', object_pairs_hook=None,
           max_buffer_size=0, ext_hook=ExtType):
    """
    Unpack a packed object, return an iterator
    Note: packed lists will be returned as tuples
    """

    return Unpacker(packed, object_hook=object_hook,
                    list_hook=list_hook,
                    use_list=use_list, encoding=encoding,
                    unicode_errors=unicode_errors,
                    object_pairs_hook=object_pairs_hook,
                    max_buffer_size=max_buffer_size,
                    ext_hook=ext_hook)


class Packer(_Packer):

    def __init__(self, default=encode,
                 encoding='utf-8',
                 unicode_errors='strict',
                 use_single_float=False,
                 autoreset=1,
                 use_bin_type=1):
        super().__init__(default=default, encoding=encoding,
                         unicode_errors=unicode_errors,
                         use_single_float=use_single_float,
                         autoreset=autoreset,
                         use_bin_type=use_bin_type)


class Unpacker(_Unpacker):

    def __init__(self, file_like=None, read_size=0, use_list=False,
                 object_hook=decode,
                 object_pairs_hook=None, list_hook=None, encoding='utf-8',
                 unicode_errors='strict', max_buffer_size=0, ext_hook=ExtType):
        super().__init__(file_like=file_like,
                         read_size=read_size,
                         use_list=use_list,
                         object_hook=object_hook,
                         object_pairs_hook=object_pairs_hook,
                         list_hook=list_hook,
                         encoding=encoding,
                         unicode_errors=unicode_errors,
                         max_buffer_size=max_buffer_size,
                         ext_hook=ext_hook)


class Iterator:

    """ manage the unpacking iteration,
        close the file on completion """

    def __init__(self, path, **kwargs):
        self.path = path
        self.kwargs = kwargs

    def __iter__(self):

        needs_closing = True
        try:

            # see if we have an actual file
            if isinstance(self.path, str):

                try:
                    path_exists = os.path.exists(self.path)
                except TypeError:
                    path_exists = False

                if path_exists:
                    fh = open(self.path, 'rb')
                else:
                    fh = BytesIO(self.path)

            else:

                if not hasattr(self.path, 'read'):
                    fh = BytesIO(self.path)

                else:

                    # a file-like
                    needs_closing = False
                    fh = self.path

            unpacker = unpack(fh)
            for o in unpacker:
                yield o
        finally:
            if needs_closing:
                fh.close()
