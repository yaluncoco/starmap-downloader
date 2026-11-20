"""Read-only S3 object addresses and CDSE SigV4 authentication.

Never sign query URLs, list private buckets, pass AWS credentials to another host, or log secrets.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import hmac
import re
import threading
from urllib.parse import quote, unquote, urlsplit

from .errors import UserError

CDSE_HOST = "eodata.dataspace.copernicus.eu"
CDSE_REGION = "default"
S1_BUCKET = "sentinel-s1-l1c"
S1_AWS_HOST = S1_BUCKET + ".s3.eu-central-1.amazonaws.com"
DEM_AWS_HOSTS = {"copernicus-dem-30m.s3.amazonaws.com", "copernicus-dem-90m.s3.amazonaws.com"}
S2_L1C_BUCKET = "sentinel-s2-l1c"
S2_L1C_AWS = S2_L1C_BUCKET + ".s3.eu-central-1.amazonaws.com"
ES_COLLECTION = "sentinel-2-c1-l2a"
ES_HOST = "e84-earth-search-sentinel-data.s3.us-west-2.amazonaws.com"
ES_ITEM = re.compile(r"(S2[A-Z])_(T[0-9]{2}[A-Z]{3})_(\d{8}T\d{6})_L2A\Z", re.ASCII)
L2A_COG_FILE = re.compile(r"(?:(?:B(?:0[1-9]|8A|11|12)|SCL|AOT|WVP|TCI|CLD_20m|SNW_20m)\.tif|metadata\.xml|tileInfo\.json|product_metadata\.xml)\Z", re.ASCII)
MGRS_TILE = re.compile(r"(?:0[1-9]|[1-5][0-9]|60)[C-HJ-NP-X][A-HJ-NP-Z]{2}\Z", re.ASCII)
L3_MIRROR_HOST = "data.source.coop"
L3_PREFIX = ("tge-labs", "sentinel-2-quarterly-cloudless-mosaics")
L3_PRODUCT = re.compile(r"Sentinel-2_mosaic_(20\d{2})_(Q[1-4])_([0-9]{2}[A-Z]{3})_([0-9]{1,2})_([0-9]{1,2})", re.ASCII)
L3_BANDS = ("B02", "B03", "B04", "B08")
WC_VERSIONS = {2020: "v100", 2021: "v200"}
WC_PRODUCTS = {
    "S2RGBNIR": ("esa-worldcover-s2", "rgbnir"),
    "NDVI": ("esa-worldcover-s2", "ndvi"),
    "SWIR": ("esa-worldcover-s2", "swir"),
    "S1VVVHratio": ("esa-worldcover-s1", "vvvhratio"),
    "Map": ("esa-worldcover", "map"),
}
WC_AWS_SUFFIX = ".s3.eu-central-1.amazonaws.com"
WC_HOSTS = frozenset(bucket + WC_AWS_SUFFIX for bucket, _ in WC_PRODUCTS.values())
WC_TILE = re.compile(r"([NS])([0-9]{2})([EW])([0-9]{3})", re.ASCII)
WC_NAME = re.compile(r"ESA_WorldCover_10m_(202[01])_(v[12]00)_([NS][0-9]{2}[EW][0-9]{3})_([A-Za-z0-9]+)\.tif", re.ASCII)
PC_LANDSAT_ACCOUNT = "landsateuwest"
PC_LANDSAT_CONTAINER = "landsat-c2"
PC_LANDSAT_HOST = PC_LANDSAT_ACCOUNT + ".blob.core.windows.net"
PC_RTC_ACCOUNT = "sentinel1euwestrtc"
PC_RTC_CONTAINER = "sentinel1-grd-rtc"
PC_RTC_HOST = PC_RTC_ACCOUNT + ".blob.core.windows.net"
RTC_POLARIZATIONS = {"DV": ("VV", "VH"), "DH": ("HH", "HV"), "SV": ("VV",), "SH": ("HH",)}
RTC_GRD = re.compile(
    r"(?P<sat>S1[ABCD])_(?P<mode>IW|EW)_GRD[FMH]_1S(?P<pol>DV|DH|SV|SH)_"
    r"(?P<start>[0-9]{8}T[0-9]{6})_(?P<stop>[0-9]{8}T[0-9]{6})_(?P<orbit>[0-9]{6})_[0-9A-F]{6}", re.ASCII,
)
S5P_ARCHIVE_HOST = "meeo-s5p.s3.eu-central-1.amazonaws.com"
S5P_NAME = re.compile(r"S5P_(OFFL|NRTI|RPRO)_(L2__[A-Z0-9_]{6})_(\d{8}T\d{6})_(\d{8}T\d{6})_(\d{5})_\d{2}_\d{6}_(\d{8}T\d{6})", re.ASCII)
SEN3_BUCKET = "meeo-s3"
SEN3_ARCHIVE_HOST = SEN3_BUCKET + ".s3.eu-central-1.amazonaws.com"
SEN3_MANIFEST = "xfdumanifest.xml"
SEN3_STREAMS = {"NT": "NTC", "NR": "NRT", "ST": "STC"}
SEN3_NAME = re.compile(
    r"(?P<sat>S3[AB])_(?P<type>[A-Z]{2}_[12]_[A-Z0-9_]{6})_"
    r"(?P<start>[0-9]{8}T[0-9]{6})_(?P<stop>[0-9]{8}T[0-9]{6})_(?P<created>[0-9]{8}T[0-9]{6})_"
    r"[0-9]{4}_[0-9]{3}_(?P<relative>[0-9]{3})_[0-9_]{4}_(?P<centre>[A-Z0-9]{3})_[OR]_(?P<stream>NT|NR|ST)_(?P<baseline>[0-9]{3})\.SEN3",
    re.ASCII,
)
SEN3_MEMBER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,119}\.nc", re.ASCII)
WIN_DEVICE = re.compile(r"(?:con|prn|aux|nul|com[0-9]|lpt[0-9])", re.IGNORECASE | re.ASCII)
PUBLIC_HOSTS = {S1_AWS_HOST, S2_L1C_AWS, *DEM_AWS_HOSTS}
ESSENTIAL_EXTENSIONS = frozenset({".zip", ".nc", ".h5", ".hdf", ".hdf5", ".h4", ".jp2", ".tif", ".tiff", ".xml", ".safe", ".json", ".txt"})
EMPTY_HASH = hashlib.sha256(b"").hexdigest()


def _object_path(href, scheme, host):
    if (not isinstance(href, str) or len(href) > 4096 or not href.startswith(f"{scheme}://{host}/")
            or any(c in href for c in ("?", "#", "\\")) or any(ord(c) < 32 or ord(c) == 127 for c in href)):
        return None
    try:
        p = urlsplit(href)
        if p.scheme != scheme or p.netloc != host or p.query or p.fragment:
            return None
        if re.search(r"%(?![a-fA-F0-9]{2})", p.path):
            return None
        parts = tuple(unquote(x, errors="strict") for x in p.path[1:].split("/"))
    except (UnicodeError, ValueError):
        return None
    if not parts or any(part in ("", ".", "..") or any(c in part for c in ("/", "\\", "%"))
                        or any(ord(c) < 32 or ord(c) == 127 for c in part) for part in parts):
        return None
    return parts


def _quoted(parts):
    return "/".join(quote(part, safe="-_.~") for part in parts)


def cdse_object(href):
    parts = _object_path(href, "s3", "eodata")
    if parts and len(parts) >= 2:
        return parts, f"https://{CDSE_HOST}/eodata/{_quoted(parts)}"
    return None


def aws_radar_object(href):
    parts = _object_path(href, "s3", S1_BUCKET)
    if parts and len(parts) >= 8 and parts[0] == "GRD":
        return parts, f"https://{S1_AWS_HOST}/{_quoted(parts)}"
    return None


def landsat_blob_object(url):
    parts = _object_path(url, "https", PC_LANDSAT_HOST)
    if (parts and len(parts) == 9 and parts[:3] == (PC_LANDSAT_CONTAINER, "level-2", "standard")
            and url == f"https://{PC_LANDSAT_HOST}/{_quoted(parts)}"
            and all(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,149}", k) for k in parts)):
        return parts
    return None


def rtc_blob_object(url):
    """Return the complete source GRD version and polarization of a canonical RTC measurement."""
    parts = _object_path(url, "https", PC_RTC_HOST)
    if not parts or len(parts) != 10 or url != f"https://{PC_RTC_HOST}/{_quoted(parts)}":
        return None
    product, _, revision = parts[7].rpartition("_")
    match = RTC_GRD.fullmatch(product)
    if not match or not re.fullmatch(r"[0-9A-F]{4}", revision):
        return None
    try:
        start, stop = (dt.datetime.strptime(match[k], "%Y%m%dT%H%M%S") for k in ("start", "stop"))
    except ValueError:
        return None
    mode, pol = match["mode"], match["pol"]
    if (not start < stop or parts[:7] != (PC_RTC_CONTAINER, "GRD", str(start.year), str(start.month), str(start.day), mode, pol)
            or parts[8] != "measurement"):
        return None
    for band in RTC_POLARIZATIONS[pol]:
        # Planetary Computer uses the iw- filename prefix even for its EW RTC sample;
        # the acquisition mode belongs to the full directory ID, not that filename prefix.
        if parts[9] == f"iw-{band.lower()}.rtc.tiff":
            return parts[7], band
    return None


def is_cdse_url(url):
    parts = _object_path(url, "https", CDSE_HOST)
    return bool(parts and len(parts) >= 3 and parts[0] == "eodata" and url == f"https://{CDSE_HOST}/{_quoted(parts)}")


def s5p_archive_parts(name):
    if not isinstance(name, str) or not name.endswith(".nc"):
        return None
    match = S5P_NAME.fullmatch(name[:-3])
    if match is None:
        return None
    stream, product, stamp, end, _, created = match.groups()
    try:
        first, last, built = [dt.datetime.strptime(t, "%Y%m%dT%H%M%S") for t in (stamp, end, created)]
    except ValueError:
        return None
    if not first < last <= built:
        return None
    return stream, product, stamp[:4], stamp[4:6], stamp[6:8], name


def sen3_member_name(name):
    return (isinstance(name, str) and (name == SEN3_MANIFEST or bool(SEN3_MEMBER.fullmatch(name)))
            and not WIN_DEVICE.fullmatch(name.split(".", 1)[0]))


def sen3_archive_parts(product):
    match = SEN3_NAME.fullmatch(product) if isinstance(product, str) else None
    if not match:
        return None
    stamp = match["start"]
    try:
        first, end, created = (dt.datetime.strptime(match[k], "%Y%m%dT%H%M%S") for k in ("start", "stop", "created"))
    except ValueError:
        return None
    if not first < end <= created:
        return None
    return SEN3_STREAMS[match["stream"]], match["sat"], match["type"], stamp[:4], stamp[4:6], stamp[6:8], product


def sen3_archive_object(url):
    parts = _object_path(url, "https", SEN3_ARCHIVE_HOST)
    if (parts and len(parts) == 8 and sen3_member_name(parts[-1]) and parts[:-1] == sen3_archive_parts(parts[-2])
            and url == f"https://{SEN3_ARCHIVE_HOST}/{_quoted(parts)}"):
        return parts
    return None


def mgrs_epsg(tile):
    if not isinstance(tile, str) or not MGRS_TILE.fullmatch(tile):
        return None
    return (32600 if tile[2] >= "N" else 32700) + int(tile[:2])


def l2a_cog_url(key, name):
    parsed = ES_ITEM.fullmatch(key) if isinstance(key, str) else None
    if not parsed or not isinstance(name, str) or not L2A_COG_FILE.fullmatch(name):
        return None
    _, tile, stamp = parsed.groups()
    if mgrs_epsg(tile[1:]) is None:
        return None
    try:
        index = dt.datetime.strptime(stamp, "%Y%m%dT%H%M%S")
    except ValueError:
        return None
    return f"https://{ES_HOST}/{ES_COLLECTION}/{int(tile[1:3])}/{tile[3]}/{tile[4:]}/{index.year}/{index.month}/{key}/{name}"


def l2a_cog_object(url):
    parts = _object_path(url, "https", ES_HOST)
    if parts and len(parts) == 8 and url == l2a_cog_url(parts[-2], parts[-1]):
        return parts[-2], parts[-1]
    return None


def l3_mirror_url(identifier, band=None):
    match = L3_PRODUCT.fullmatch(identifier) if isinstance(identifier, str) else None
    if not match or not mgrs_epsg(match[3]) or int(match[1]) < 2017 or band is not None and band not in L3_BANDS:
        return None
    year, quarter, tile, i, j = match.groups()
    if any(str(int(index)) != index for index in (i, j)):
        return None
    name = band + ".tif" if band else identifier + ".json"
    return f"https://{L3_MIRROR_HOST}/{'/'.join(L3_PREFIX)}/{year}/{quarter}/{tile}_{i}_{j}/{name}"


def l3_mirror_object(url):
    parts = _object_path(url, "https", L3_MIRROR_HOST)
    if parts and len(parts) == 6 and parts[:2] == L3_PREFIX and parts[-1].endswith(".tif"):
        band = parts[-1][:-4]
        identifier = "Sentinel-2_mosaic_" + "_".join(parts[2:5])
        if url == l3_mirror_url(identifier, band):
            return identifier, band
    return None


def worldcover_tile(tile, side):
    match = WC_TILE.fullmatch(tile) if isinstance(tile, str) else None
    if match is None:
        return None
    ns, lat, ew, lon = match.groups()
    south, west = int(lat) * (1 if ns == "N" else -1), int(lon) * (1 if ew == "E" else -1)
    if ((south == 0 and ns != "N") or (west == 0 and ew != "E") or south % side or west % side
            or not -90 <= south <= 90-side or not -180 <= west <= 180-side):
        return None
    return west, south, west+side, south+side


def worldcover_prefix(year, tile, asset):
    """S3 ListObjectsV2 is restricted to an exact product/year/latitude row."""
    if year not in WC_VERSIONS or asset not in WC_PRODUCTS or not worldcover_tile(tile, 3 if asset == "Map" else 1):
        return None
    bucket, directory = WC_PRODUCTS[asset]
    version = WC_VERSIONS[year]
    if asset == "Map":
        return bucket, f"{version}/{year}/{directory}/ESA_WorldCover_10m_{year}_{version}_{tile[:3]}"
    return bucket, f"{directory}/{year}/{tile[:3]}/"


def worldcover_url(year, tile, asset):
    listing = worldcover_prefix(year, tile, asset)
    if listing is None:
        return None
    bucket, prefix = listing
    name = f"ESA_WorldCover_10m_{year}_{WC_VERSIONS[year]}_{tile}_{asset}.tif"
    return f"https://{bucket}{WC_AWS_SUFFIX}/{prefix.rsplit('/', 1)[0]}/{name}"


def worldcover_object(url):
    if not isinstance(url, str):
        return None
    for host in WC_HOSTS:
        parts = _object_path(url, "https", host)
        if parts and (match := WC_NAME.fullmatch(parts[-1])) is not None:
            year, _, tile, asset = match.groups()
            if url == worldcover_url(int(year), tile, asset):
                return int(year), tile, asset
    return None


def is_public_url(url):
    if worldcover_object(url) is not None:
        return True
    if isinstance(url, str) and url.startswith(f"https://{L3_MIRROR_HOST}/"):
        return l3_mirror_object(url) is not None
    if isinstance(url, str) and url.startswith(f"https://{ES_HOST}/"):
        return l2a_cog_object(url) is not None
    if isinstance(url, str) and url.startswith(f"https://{SEN3_ARCHIVE_HOST}/"):
        return sen3_archive_object(url) is not None
    s5p = _object_path(url, "https", S5P_ARCHIVE_HOST)
    if s5p:
        return s5p == s5p_archive_parts(s5p[-1]) and url == f"https://{S5P_ARCHIVE_HOST}/{_quoted(s5p)}"
    for host in PUBLIC_HOSTS:
        parts = _object_path(url, "https", host)
        if parts and len(parts) >= 2 and url == f"https://{host}/{_quoted(parts)}":
            return True
    return False


def _sign_get(url, headers, key_id, secret, region, now):
    """AWS SigV4 for a canonical, already URI-encoded, query-free GET object URL."""
    destination = urlsplit(url)
    time = now.astimezone(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    day = time[:8]
    signed_headers = {"host": destination.netloc, "x-amz-date": time, "x-amz-content-sha256": EMPTY_HASH}
    for name, value in headers.items():
        if name.lower() in ("range", "if-match"):
            signed_headers[name.lower()] = " ".join(value.split())
    order = sorted(signed_headers)
    names = ";".join(order)
    fields = "".join(f"{name}:{signed_headers[name]}\n" for name in order)
    request = f"GET\n{destination.path}\n\n{fields}\n{names}\n{EMPTY_HASH}"
    scope = f"{day}/{region}/s3/aws4_request"
    text = f"AWS4-HMAC-SHA256\n{time}\n{scope}\n{hashlib.sha256(request.encode('utf-8')).hexdigest()}"
    signing = ("AWS4" + secret).encode("ascii")
    for part in (day, region, "s3", "aws4_request"):
        signing = hmac.digest(signing, part.encode("ascii"), "sha256")
    signature = hmac.new(signing, text.encode("ascii"), "sha256").hexdigest()
    return {**headers, "X-Amz-Date": time, "X-Amz-Content-Sha256": EMPTY_HASH,
            "Authorization": f"AWS4-HMAC-SHA256 Credential={key_id}/{scope},SignedHeaders={names},Signature={signature}"}


class CDSES3Auth:
    def __init__(self):
        self._lock = threading.RLock()
        self._keys = None

    @property
    def connected(self):
        with self._lock:
            return self._keys is not None

    def clear(self):
        with self._lock:
            self._keys = None

    def connect(self, key_id, secret):
        key_id = key_id.strip() if isinstance(key_id, str) else ""
        secret = secret.strip() if isinstance(secret, str) else ""
        if not re.fullmatch(r"[A-Za-z0-9_+=-]{8,128}", key_id) or not re.fullmatch(r"[!-~]{16,256}", secret):
            raise UserError("请输入 CDSE S3 Keys Manager 生成的 Access Key 和 Secret Key，不是 Sentinel Hub 的 OAuth 凭据。")
        with self._lock:
            self._keys = key_id, secret
        # A loaded pair is not evidence of product authorization until the first signed request.
        return {"connected": True, "verified": False}

    def __call__(self, request):
        # requests invokes this on each prepared request, including HTTP retries. This both
        # refreshes the timestamp and keeps credentials from being overridden by netrc.
        if request.method != "GET":
            raise UserError("CDSE S3 密钥仅允许签名原始文件 GET 请求。")
        request.headers.update(self.headers(request.url, request.headers))
        return request

    def headers(self, url, headers):
        if not is_cdse_url(url):
            raise UserError("S3 签名仅可用于固定 CDSE 对象地址。")
        with self._lock:
            if self._keys is None:
                raise UserError("请先在软件中连接 CDSE S3 密钥。")
            key_id, secret = self._keys
        return _sign_get(url, headers, key_id, secret, CDSE_REGION, dt.datetime.now(dt.timezone.utc))
