"""Explicit, individually source-read exact lookups; no question templates.
Qualified names identify genuine public/source APIs, not fabricated identifiers.
Filename/path requests bind the returned source identity plus actual basename.
"""
from pathlib import Path
import json

entries = []

def add(path, rows):
    for question, tag, view, anchor, fact in rows:
        entries.append({'repository': 'cpython', 'path': path, 'q': question,
                        'tags': [tag], 'view_id': view, 'anchor': anchor, 'fact': fact})

add('Lib/hashlib.py', [
    ('hashlib.py', 'filename', 'basename', 'hashlib.py',
     'The requested source file has the exact basename hashlib.py.'),
    ('hashlib.sha256', 'mixed_identifier', 'body',
     "__always_supported = ('md5', 'sha1', 'sha224', 'sha256', 'sha384', 'sha512',",
     'sha256 is listed among the hashing algorithms that hashlib always supports.'),
    ('hashlib.sha3_512', 'mixed_identifier', 'body',
     "cache['sha3_512'] = _sha3.sha3_512",
     'The built-in constructor cache binds sha3_512 to the corresponding _sha3 implementation.'),
    ('algorithms_guaranteed', 'punctuated_identifier', 'body',
     'algorithms_guaranteed = set(__always_supported)',
     'algorithms_guaranteed is the set of the always-supported algorithm names.'),
    ('file_digest', 'punctuated_identifier', 'body',
     'def file_digest(fileobj, digest, /, *, _bufsize=2**18):\n    """Hash the contents of a file-like object. Returns a digest object.',
     'file_digest hashes a file-like object and returns a digest object.'),
])

add('Lib/base64.py', [
    ('base64.py', 'filename', 'basename', 'base64.py',
     'The requested encoding implementation has the exact basename base64.py.'),
    ('base64.b64encode', 'mixed_identifier', 'body',
     'def b64encode(s, altchars=None):\n    """Encode the bytes-like object s using Base64 and return a bytes object.',
     'b64encode encodes bytes-like input as Base64 and returns bytes.'),
    ('base64.b64decode', 'mixed_identifier', 'body',
     'def b64decode(s, altchars=None, validate=False):\n    """Decode the Base64 encoded bytes-like object or ASCII string s.',
     'b64decode accepts Base64-encoded bytes-like input or an ASCII string.'),
    ('_bytes_from_decode_data', 'punctuated_identifier', 'body',
     "def _bytes_from_decode_data(s):\n    if isinstance(s, str):\n        try:\n            return s.encode('ascii')",
     'The decoding-input helper converts string input to ASCII bytes.'),
    ('_urlsafe_encode_translation', 'punctuated_identifier', 'body',
     "_urlsafe_encode_translation = bytes.maketrans(b'+/', b'-_')",
     'The URL-safe encoding translation maps plus and slash to hyphen and underscore.'),
])

add('Lib/ipaddress.py', [
    ('ipaddress.py', 'filename', 'basename', 'ipaddress.py',
     'The address-manipulation module requested has basename ipaddress.py.'),
    ('ipaddress.IPv4Address', 'mixed_identifier', 'body',
     'class IPv4Address(_BaseV4, _BaseAddress):\n\n    """Represent and manipulate single IPv4 Addresses."""',
     'IPv4Address represents and manipulates one IPv4 address.'),
    ('ipaddress.IPv6Address', 'mixed_identifier', 'body',
     'class IPv6Address(_BaseV6, _BaseAddress):\n\n    """Represent and manipulate single IPv6 Addresses."""',
     'IPv6Address represents and manipulates one IPv6 address.'),
    ('ip_address', 'punctuated_identifier', 'body',
     'def ip_address(address):\n    """Take an IP string/int and return an object of the correct type.',
     'ip_address chooses the appropriate address object type from string or integer input.'),
    ('ip_network', 'punctuated_identifier', 'body',
     'def ip_network(address, strict=True):\n    """Take an IP string/int and return an object of the correct type.',
     'ip_network accepts address input with a strict argument defaulting to true.'),
])

add('Lib/ssl.py', [
    ('ssl.py', 'filename', 'basename', 'ssl.py',
     'The requested TLS wrapper source has basename ssl.py.'),
    ('ssl.TLSVersion.TLSv1_2', 'mixed_identifier', 'body',
     '    TLSv1_2 = _ssl.PROTO_TLSv1_2',
     'TLSVersion maps TLSv1_2 to the low-level TLS 1.2 protocol constant.'),
    ('ssl.TLSVersion.TLSv1_3', 'mixed_identifier', 'body',
     '    TLSv1_3 = _ssl.PROTO_TLSv1_3',
     'TLSVersion maps TLSv1_3 to the low-level TLS 1.3 protocol constant.'),
    ('create_default_context', 'punctuated_identifier', 'body',
     'def create_default_context(purpose=Purpose.SERVER_AUTH, *, cafile=None,\n                           capath=None, cadata=None):\n    """Create a SSLContext object with default settings.',
     'create_default_context constructs an SSLContext with default settings and accepts certificate authority inputs.'),
    ('DER_cert_to_PEM_cert', 'punctuated_identifier', 'body',
     'def DER_cert_to_PEM_cert(der_cert_bytes):\n    """Takes a certificate in binary DER format and returns the\n    PEM version of it as a string."""',
     'DER_cert_to_PEM_cert converts binary DER certificate bytes to a PEM string.'),
])

add('Lib/uuid.py', [
    ('uuid.py', 'filename', 'basename', 'uuid.py',
     'The UUID implementation requested is the file named uuid.py.'),
    ('uuid.uuid3', 'mixed_identifier', 'body',
     'def uuid3(namespace, name):\n    """Generate a UUID from the MD5 hash of a namespace UUID and a name."""',
     'uuid3 derives a UUID from the MD5 hash of a namespace UUID and a name.'),
    ('uuid.uuid4', 'mixed_identifier', 'body',
     'def uuid4():\n    """Generate a random UUID."""\n    return UUID(bytes=os.urandom(16), version=4)',
     'uuid4 generates a random version-four UUID from sixteen operating-system random bytes.'),
    ('NAMESPACE_DNS', 'punctuated_identifier', 'body',
     "NAMESPACE_DNS = UUID('6ba7b810-9dad-11d1-80b4-00c04fd430c8')",
     'The DNS namespace UUID constant is 6ba7b810-9dad-11d1-80b4-00c04fd430c8.'),
    ('NAMESPACE_URL', 'punctuated_identifier', 'body',
     "NAMESPACE_URL = UUID('6ba7b811-9dad-11d1-80b4-00c04fd430c8')",
     'The URL namespace UUID constant is 6ba7b811-9dad-11d1-80b4-00c04fd430c8.'),
])

add('Lib/zipfile/__init__.py', [
    ('Lib/zipfile/__init__.py', 'path', 'basename', '__init__.py',
     'The requested zipfile package source is its __init__.py initializer; the bound source path identifies the package.'),
    ('zipfile/__init__.py', 'path', 'basename', '__init__.py',
     'The zipfile-relative initializer request points to a file with basename __init__.py.'),
    ('ZIP64_LIMIT', 'mixed_identifier', 'body',
     'ZIP64_LIMIT = (1 << 31) - 1',
     'ZIP64_LIMIT is defined as two to the thirty-first power minus one.'),
    ('ZIP_MAX_COMMENT', 'punctuated_identifier', 'body',
     'ZIP_MAX_COMMENT = (1 << 16) - 1',
     'The maximum ZIP comment length constant is two to the sixteenth power minus one.'),
    ('is_zipfile', 'punctuated_identifier', 'body',
     'def is_zipfile(filename):\n    """Quickly see if a file is a ZIP file by checking the magic number.',
     'is_zipfile checks the magic number to determine whether a file is a ZIP file.'),
])

add('Modules/zlibmodule.c', [
    ('zlibmodule.c', 'filename', 'basename', 'zlibmodule.c',
     'The requested compression extension implementation is named zlibmodule.c.'),
    ('zlib.adler32', 'mixed_identifier', 'body',
     'zlib.adler32\n\n    data: Py_buffer\n    value: unsigned_int(bitwise=True) = 1\n        Starting value of the checksum.\n    /\n\nCompute an Adler-32 checksum of data.',
     'adler32 computes an Adler-32 checksum and defaults the starting checksum value to one.'),
    ('zlib.crc32', 'mixed_identifier', 'body',
     'zlib.crc32 -> unsigned_int\n\n    data: Py_buffer\n    value: unsigned_int(bitwise=True) = 0\n        Starting value of the checksum.\n    /\n\nCompute a CRC-32 checksum of data.',
     'crc32 computes a CRC-32 checksum and defaults the starting checksum value to zero.'),
    ('zlib.decompressobj', 'punctuated_identifier', 'body',
     'zlib.decompressobj\n\n    wbits: int(c_default="MAX_WBITS") = MAX_WBITS\n        The window buffer size and container format.\n    zdict: object(c_default="NULL") = b\'\'\n        The predefined compression dictionary.  This must be the same\n        dictionary as used by the compressor that produced the input data.\n\nReturn a decompressor object.',
     'decompressobj returns a decompressor and its predefined dictionary must match the compressor dictionary.'),
    ('zlib.Decompress.flush', 'punctuated_identifier', 'body',
     'zlib.Decompress.flush\n\n    cls: defining_class\n    length: Py_ssize_t(c_default="DEF_BUF_SIZE") = zlib.DEF_BUF_SIZE\n        the initial size of the output buffer.\n    /\n\nReturn a bytes object containing any remaining decompressed data.',
     'The decompressor flush method returns any remaining decompressed data as bytes.'),
])

add('Lib/email/base64mime.py', [
    ('base64mime.py', 'filename', 'basename', 'base64mime.py',
     'The mail transfer-encoding implementation has basename base64mime.py.'),
    ('Lib/email/base64mime.py', 'path', 'basename', 'base64mime.py',
     'The requested email-package source path is bound to the file with basename base64mime.py.'),
    ('header_length', 'punctuated_identifier', 'body',
     'def header_length(bytearray):\n    """Return the length of s when it is encoded with base64."""',
     'header_length calculates the length after Base64 encoding.'),
    ('header_encode', 'punctuated_identifier', 'body',
     'def header_encode(header_bytes, charset=\'iso-8859-1\'):\n    """Encode a single header line with Base64 encoding in a given charset.',
     'header_encode Base64-encodes a single header line, using the given charset and defaulting to iso-8859-1.'),
    ('body_decode', 'punctuated_identifier', 'body',
     '# For convenience and backwards compatibility w/ standard base64 module\nbody_decode = decode\ndecodestring = decode',
     'body_decode is a compatibility alias for the decode function.'),
])

add('Lib/codecs.py', [
    ('codecs.py', 'filename', 'basename', 'codecs.py',
     'The registry and codec-helper source file is named codecs.py.'),
    ('BOM_UTF8', 'mixed_identifier', 'body',
     "# UTF-8\nBOM_UTF8 = b'\\xef\\xbb\\xbf'",
     'The UTF-8 byte-order mark is the byte sequence ef bb bf.'),
    ('BOM_UTF32_BE', 'mixed_identifier', 'body',
     "# UTF-32, big endian\nBOM_UTF32_BE = b'\\x00\\x00\\xfe\\xff'",
     'The big-endian UTF-32 byte-order mark is 00 00 fe ff.'),
    ('codecs.getincrementalencoder', 'punctuated_identifier', 'body',
     'def getincrementalencoder(encoding):\n\n    """ Lookup up the codec for the given encoding and return\n        its IncrementalEncoder class or factory function.',
     'getincrementalencoder looks up an encoding and returns its incremental encoder class or factory.'),
    ('codecs.getincrementaldecoder', 'punctuated_identifier', 'body',
     'def getincrementaldecoder(encoding):\n\n    """ Lookup up the codec for the given encoding and return\n        its IncrementalDecoder class or factory function.',
     'getincrementaldecoder looks up an encoding and returns its incremental decoder class or factory.'),
])

add('Lib/ctypes/__init__.py', [
    ('Lib/ctypes/__init__.py', 'path', 'basename', '__init__.py',
     'The ctypes package source at the requested path has basename __init__.py.'),
    ('ctypes/__init__.py', 'path', 'basename', '__init__.py',
     'The package-relative ctypes initializer is the bound __init__.py source file.'),
    ('ctypes.c_int64', 'mixed_identifier', 'body',
     'for kind in [c_short, c_int, c_long, c_longlong]:\n    if sizeof(kind) == 2: c_int16 = kind\n    elif sizeof(kind) == 4: c_int32 = kind\n    elif sizeof(kind) == 8: c_int64 = kind',
     'c_int64 is selected from the signed C integer kinds whose size is eight bytes.'),
    ('create_string_buffer', 'punctuated_identifier', 'body',
     'def create_string_buffer(init, size=None):\n    """create_string_buffer(aBytes) -> character array\n    create_string_buffer(anInteger) -> character array\n    create_string_buffer(aBytes, anInteger) -> character array',
     'create_string_buffer accepts bytes, an integer size, or bytes plus an explicit size and creates a character array.'),
    ('c_void_p', 'punctuated_identifier', 'body',
     'class c_void_p(_SimpleCData):\n    _type_ = "P"',
     'c_void_p is the simple C-data wrapper with pointer type code P.'),
])

add('Lib/random.py', [
    ('random.py', 'filename', 'basename', 'random.py',
     'The pseudo-random generator implementation file has basename random.py.'),
    ('_sha512', 'mixed_identifier', 'body',
     '        elif version == 2 and isinstance(a, (str, bytes, bytearray)):\n            if isinstance(a, str):\n                a = a.encode()\n            a = int.from_bytes(a + _sha512(a).digest())',
     'Version-two string and byte seeds are extended with their SHA-512 digest before conversion to an integer.'),
    ('NV_MAGICCONST', 'punctuated_identifier', 'body',
     'NV_MAGICCONST = 4 * _exp(-0.5) / _sqrt(2.0)',
     'NV_MAGICCONST is defined as four times exp(-0.5) divided by sqrt(2).'),
    ('RECIP_BPF', 'punctuated_identifier', 'body',
     'BPF = 53        # Number of bits in a float\nRECIP_BPF = 2 ** -BPF',
     'RECIP_BPF is two raised to minus the fifty-three float bits.'),
    ('Random.getstate', 'punctuated_identifier', 'body',
     '    def getstate(self):\n        """Return internal state; can be passed to setstate() later."""\n        return self.VERSION, super().getstate(), self.gauss_next',
     'Random.getstate returns the version, underlying generator state and cached Gaussian value.'),
])

add('Lib/xmlrpc/client.py', [
    ('client.py', 'filename', 'basename', 'client.py',
     'The requested XML-RPC client implementation is the bound source with basename client.py.'),
    ('TRANSPORT_ERROR', 'punctuated_identifier', 'body',
     'TRANSPORT_ERROR   = -32300',
     'The XML-RPC transport error constant is minus 32300.'),
    ('_MultiCallMethod', 'punctuated_identifier', 'body',
     'class _MultiCallMethod:\n    # some lesser magic to store calls made to a MultiCall object\n    # for batch execution',
     'The private _MultiCallMethod class stores calls made to a MultiCall object for batch execution.'),
    ('gzip_encode', 'punctuated_identifier', 'body',
     'def gzip_encode(data):\n    """data -> gzip encoded data\n\n    Encode data using the gzip content encoding as described in RFC 1952',
     'gzip_encode converts input data into the gzip content encoding from RFC 1952.'),
    ('gzip_decode', 'punctuated_identifier', 'body',
     'def gzip_decode(data, max_decode=20971520):\n    """gzip encoded data -> unencoded data',
     'gzip_decode decodes gzip data and defaults the maximum decoded size to 20971520 bytes.'),
])

assert len(entries) == 60
out = Path(__file__).with_name('lookup-authoring.json')
out.write_text(json.dumps(entries, ensure_ascii=False, indent=2) + '\n')
print(f'wrote {len(entries)} explicit lookup questions to {out}')
