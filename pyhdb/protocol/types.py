# Copyright 2014 SAP SE
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import sys
import re
import struct
import binascii
import decimal
import logging
import datetime
from weakref import WeakValueDictionary

from pyhdb.protocol.constants import type_codes
from pyhdb.exceptions import InterfaceError
from pyhdb._compat import PY2, PY3, with_metaclass, iter_range, int_types, \
    string_types, byte_type, text_type


recv_log = logging.getLogger('receive')
debug = recv_log.debug

# Dictionary: keys: numeric type_code, values: Type-(sub)classes (from below)
by_type_code = WeakValueDictionary()
# Dictionary: keys: Python type classes, values: Type-(sub)classes (from below)
by_python_type = WeakValueDictionary()

PY26 = PY2 and sys.version_info[1] == 6


class TypeMeta(type):
    """
    Meta class for Type classes.
    """

    @staticmethod
    def _add_type_to_type_code_mapping(type_class, type_code):
        if not 0 <= type_code <= 127:
            raise InterfaceError(
                "%s type type_code must be between 0 and 127" %
                type_class.__name__
            )
        by_type_code[type_code] = type_class

    def __new__(cls, name, bases, attrs):
        type_class = super(TypeMeta, cls).__new__(cls, name, bases, attrs)

        # populate by_type_code mapping
        if hasattr(type_class, "type_code"):
            if isinstance(type_class.type_code, (tuple, list)):
                for type_code in type_class.type_code:
                    TypeMeta._add_type_to_type_code_mapping(type_class, type_code)
            else:
                TypeMeta._add_type_to_type_code_mapping(
                    type_class, type_class.type_code
                )

        # populate by_python_type mapping
        if hasattr(type_class, "python_type"):
            if isinstance(type_class.python_type, (tuple, list)):
                for typ in type_class.python_type:
                    by_python_type[typ] = type_class
            else:
                by_python_type[type_class.python_type] = type_class

        return type_class


class Type(with_metaclass(TypeMeta, object)):
    """Base class for all types"""


class NoneType(Type):

    python_type = None.__class__

    @classmethod
    def to_sql(cls, _):
        return text_type("NULL")


class _IntType(Type):

    @classmethod
    def from_resultset(cls, payload, connection=None):
        if payload.read(1) == b"\x01":
            # x01 indicates that there is a real value available to be read
            return cls._struct.unpack(payload.read(cls._struct.size))[0]
        else:
            # Value is Null
            return None

    @classmethod
    def prepare(cls, value):
        if value is None:
            pfield = struct.pack('b', 0)
        else:
            pfield = struct.pack('b', cls.type_code)
            pfield += cls._struct.pack(value)
        return pfield


class TinyInt(_IntType):

    type_code = type_codes.TINYINT
    _struct = struct.Struct("B")


class SmallInt(_IntType):

    type_code = type_codes.SMALLINT
    _struct = struct.Struct("h")


class Int(_IntType):

    type_code = type_codes.INT
    python_type = int_types
    _struct = struct.Struct("i")

    @classmethod
    def to_sql(cls, value):
        return text_type(value)


class BigInt(_IntType):

    type_code = type_codes.BIGINT
    _struct = struct.Struct("l")


class Decimal(Type):

    type_code = type_codes.DECIMAL
    python_type = decimal.Decimal

    @classmethod
    def from_resultset(cls, payload, connection=None):
        payload = bytearray(payload.read(16))
        payload.reverse()

        if payload[0] == 0x70:
            return None

        sign = payload[0] >> 7
        exponent = ((payload[0] & 0x7F) << 7) | ((payload[1] & 0xFE) >> 1)
        exponent = exponent - 6176
        mantissa = (payload[1] & 0x01) << 112

        x = 104
        for i in iter_range(2, 16):
            mantissa = mantissa | ((payload[i]) << x)
            x -= 8

        number = pow(-1, sign) * decimal.Decimal(10) ** exponent * mantissa
        return number

    @classmethod
    def to_sql(cls, value):
        return text_type(value)


class Real(Type):

    type_code = type_codes.REAL
    _struct = struct.Struct("<f")

    @classmethod
    def from_resultset(cls, payload, connection=None):
        payload = payload.read(8)
        if payload == b"\xFF\xFF\xFF\xFF":
            return None
        return cls._struct.unpack(payload)[0]


class Double(_IntType):

    type_code = type_codes.DOUBLE
    python_type = float
    _struct = struct.Struct("<d")

    @classmethod
    def from_resultset(cls, payload, connection=None):
        payload = payload.read(8)
        if payload == b"\xFF\xFF\xFF\xFF\xFF\xFF\xFF\xFF":
            return None
        return cls._struct.unpack(payload)[0]

    @classmethod
    def to_sql(cls, value):
        return text_type(value)


class String(Type):

    type_code = (type_codes.CHAR, type_codes.VARCHAR, type_codes.NCHAR, type_codes.NVARCHAR,
                 type_codes.STRING, type_codes.NSTRING)
    python_type = string_types

    ESCAPE_REGEX = re.compile(r"[\']")
    ESCAPE_MAP = {"'": "''"}

    @staticmethod
    def get_length(payload):
        length_indicator = struct.unpack('B', payload.read(1))[0]
        if length_indicator <= 245:
            length = length_indicator
        elif length_indicator == 246:
            length = struct.unpack('h', payload.read(2))[0]
        elif length_indicator == 247:
            length = struct.unpack('i', payload.read(4))[0]
        elif length_indicator == 255:
            return None
        else:
            raise InterfaceError("Unknown length inidcator")
        return length

    @classmethod
    def from_resultset(cls, payload, connection=None):
        length = String.get_length(payload)
        if length is None:
            return None
        return payload.read(length).decode('cesu-8')

    @classmethod
    def to_sql(cls, value):
        return "'%s'" % cls.ESCAPE_REGEX.sub(
            lambda match: cls.ESCAPE_MAP.get(match.group(0)),
            value
        )

    @classmethod
    def prepare(cls, value, type_code=type_codes.CHAR):
        pfield = struct.pack('b', type_code)
        if value is None:
            # length indicator
            pfield += struct.pack('b', 255)
        else:
            value = value.encode('cesu-8')
            length = len(value)
            # length indicator
            if length <= 245:
                pfield += struct.pack('b', length)
            elif length <= 32767:
                pfield += struct.pack('b', 246)
                pfield += struct.pack('h', length)
            else:
                pfield += struct.pack('b', 247)
                pfield += struct.pack('i', length)
            pfield += value
        return pfield


class Binary(Type):

    type_code = (type_codes.BINARY, type_codes.VARBINARY, type_codes.BSTRING)
    python_type = byte_type

    @classmethod
    def from_resultset(cls, payload, connection=None):
        length = String.get_length(payload)
        if length is None:
            return None
        return byte_type(payload.read(length))

    @classmethod
    def to_sql(cls, value):
        if PY26:
            value = bytes(value)
        value = binascii.hexlify(value)
        if PY3:
            value = value.decode('ascii')
        return "'%s'" % value


class Date(Type):

    type_code = type_codes.DATE
    python_type = datetime.date
    _struct = struct.Struct("<hbh")

    @classmethod
    def from_resultset(cls, payload, connection=None):
        payload = bytearray(payload.read(4))
        if not payload[1] & 0x80:
            return None

        year = payload[0] | (payload[1] & 0x3F) << 8
        month = payload[2] + 1
        day = payload[3]
        return cls.python_type(year, month, day)

    @classmethod
    def to_sql(cls, value):
        return "'%s'" % value.isoformat()

    @classmethod
    def prepare(cls, value):
        """Pack datetime value into proper binary format"""
        # According to the docs setting year to 0x8000 indicates a NULL value for a date object
        year = 0x8000 if value is None else value.year
        pfield = struct.pack('b', cls.type_code)
        pfield += cls._struct.pack(year, value.month, value.day)
        return pfield

    @classmethod
    def to_daydate(cls, *argv):
        """
        Convert date to Julian day (DAYDATE)
        """
        argc = len(argv)
        if argc == 3:
            year, month, day = argv
        elif argc == 1:
            dval = argv[0]
            try:
                year = dval.year
                month = dval.month
                day = dval.day
            except AttributeError:
                raise InterfaceError("Unsupported python date input: %s (%s)" % (str(dval), dval.__class__))
        else:
            raise InterfaceError("Date.to_datetime does not support %d arguments." % argc)

        TURN_OF_ERAS = 1721424

        if month < 3:
            year -= 1
            month += 12

        if ((year > 1582) or
                (year == 1582 and month > 10) or
                (year == 1582 and month == 10 and day >= 15)):
            A = int(year / 100)
            B = int(A / 4)
            C = 2 - A + B
        else:
            C = 0

        E = int(365.25 * (year + 4716))
        F = int(30.6001 * (month + 1))
        Z = C + day + E + F - 1524
        return Z + 1 - TURN_OF_ERAS


class Time(Type):

    type_code = type_codes.TIME
    python_type = datetime.time
    _struct = struct.Struct("<bbH")

    @classmethod
    def from_resultset(cls, payload, connection=None):
        hour, minute, millisec = cls._struct.unpack(payload.read(4))
        if not hour & 0x80:
            return None

        hour = hour & 0x7f
        second, millisec = divmod(millisec, 1000)
        return cls.python_type(hour, minute, second, millisec * 1000)

    @classmethod
    def to_sql(cls, value):
        return "'%s'" % value.strftime("%H:%M:%S")


class Timestamp(Type):

    type_code = type_codes.TIMESTAMP
    python_type = datetime.datetime

    @classmethod
    def from_resultset(cls, payload, connection=None):
        date = Date.from_resultset(payload)
        time = Time.from_resultset(payload)

        if date is None or time is None:
            return None

        return datetime.datetime.combine(date, time)

    @classmethod
    def to_sql(cls, value):
        return "'%s.%s'" % (value.strftime("%Y-%m-%d %H:%M:%S"), value.microsecond)


class MixinLobType(object):
    """Base class for all LOB types"""
    @classmethod
    def from_resultset(cls, payload, connection=None):
        # to avoid circular import the 'lobs' module has to be imported here:
        from . import lobs
        return lobs.from_payload(cls.type_code, payload, connection)


class ClobType(Type, MixinLobType):
    """CLOB type class"""
    type_code = type_codes.CLOB


class NClobType(Type, MixinLobType):
    """NCLOB type class"""
    type_code = type_codes.NCLOB


class BlobType(Type, MixinLobType):
    """BLOB type class"""
    type_code = type_codes.BLOB


def escape(value):
    """
    Escape a single value.
    """

    if isinstance(value, (tuple, list)):
        return "(" + ", ".join([escape(arg) for arg in value]) + ")"
    else:
        typ = by_python_type.get(value.__class__)
        if typ is None:
            raise InterfaceError(
                "Unsupported python input: %s (%s)" % (value, value.__class__)
            )

        return typ.to_sql(value)


def escape_values(values):
    """
    Escape multiple values from a list, tuple or dict.
    """
    if isinstance(values, (tuple, list)):
        return tuple([escape(value) for value in values])
    elif isinstance(values, dict):
        return dict([
            (key, escape(value)) for (key, value) in values.items()
        ])
    else:
        raise InterfaceError("escape_values expects list, tuple or dict")
