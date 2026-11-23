from __future__ import annotations

import copy
import hashlib
import io
import json
import re
import struct
import threading
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

import pytest
import requests
from shapely.geometry import Polygon, box

from conftest import StubResponse
from core import open_slc, raw_download, slc_archive
from core.errors import Cancelled, UserError
from core.open_slc import ASF_SEARCH, SLC_PROOF, check_slc_target, search_slc, slc_ref

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "slc-asf-357c.json"
SINGLE = "S1A_IW_SLC__1SSV_20141101T100821_20141101T100834_003086_00388F_357C"
DUAL = "S1A_IW_SLC__1SDV_20230910T100156_20230910T100208_050263_060CF3_3977"
DATE = "2014-11-01"
AOI = box(123.70, -8.145, 123.74, -8.10)
TAG = '"0123456789abcdef0123456789abcdef-10"'


def md5(data):
    return hashlib.md5(data, usedforsecurity=False).hexdigest()


def target(payload, product=SINGLE):
    ref = slc_ref(product)
    return {"key": product, "uuid": None, "asset": "zip", "bytes": len(payload), "name": product+".zip", "url": ref.url,
            "transport": "public", "provider_md5": md5(payload), "names": [product],
            "identity": {"kind": SLC_PROOF, "product": product, "archive_etag": TAG}}


def fixture_asf():
    return json.loads(FIXTURE.read_bytes())


class SearchStub:
    def __init__(self, doc=None, code=200, **head):
        self.doc = doc or fixture_asf()
        self.calls = []
        self.status = code
        self.head = {"Content-Length": "966228993", "ETag": TAG, "Content-Type": "binary/octet-stream", **head}

    def request(self, method, url, **kw):
        self.calls.append((method, url, kw))
        if method == "GET":
            assert url == ASF_SEARCH and kw["params"]["processingLevel"] == "SLC" and kw["params"]["beamMode"] == "IW"
            assert kw["params"]["start"] == DATE+"T00:00:00Z" and kw["params"]["end"] == "2014-11-02T00:00:00Z"
            return StubResponse(content=json.dumps(self.doc).encode())
        assert method == "HEAD" and url == slc_ref(SINGLE).url
        return StubResponse(status=self.status, headers=self.head)

    def close(self):
        pass


def test_archive_search_and_strict_plan(monkeypatch):
    asf = SearchStub()
    monkeypatch.setattr(open_slc, "HttpClient", lambda: asf)
    row, = search_slc(AOI, DATE, DATE)
    assert row["coverage_percent"] == 100 and row["tiles"] == ["IW · VV"] and row["date"] == DATE
    assert [method for method, _, _ in asf.calls] == ["GET", "HEAD"]
    plan = raw_download.raw_plan(AOI,[row],{"mode":"original", "transport":"public"})
    assert not plan["requires"] and plan["estimated_bytes"] == 966228993 and plan["folder_name"].startswith("Sentinel-1-SLC_")
    assert check_slc_target(plan["targets"][0]).product == SINGLE


@pytest.mark.parametrize("head",[{"code":404},{"Content-Length":"123"},{"Content-Type":"text/html"},{"ETag":"W/"+TAG}])
def test_asf_row_alone_is_not_downloadable(monkeypatch,head):
    stub = SearchStub(**head)
    monkeypatch.setattr(open_slc, "HttpClient", lambda: stub)
    assert not search_slc(AOI, DATE, DATE)
    assert [r[0] for r in stub.calls] == ["GET", "HEAD"]


@pytest.mark.parametrize("field,value",[("md5sum", None),("fileName", "wrong.zip"),("orbit", 3087),
                                       ("startTime", "2014-11-02T00:00:00Z"),("processingLevel", "GRD_HD"),
                                       ("polarization", "VV+VH"),("fileID", SINGLE)])
def test_no_mismatched_scene_or_fallback(monkeypatch,field,value):
    doc = fixture_asf()
    doc["features"] = [f for f in doc["features"] if f["properties"].get("fileID") == SINGLE+"-SLC"]
    doc["features"][0]["properties"][field] = value
    stub = SearchStub(doc)
    monkeypatch.setattr(open_slc,"HttpClient", lambda: stub)
    assert not search_slc(AOI, DATE, DATE)
    assert [r[0] for r in stub.calls] == ["GET"]


def test_envelope_only_candidate_in_aoi_hole_and_future_day(monkeypatch):
    stub = SearchStub()
    monkeypatch.setattr(open_slc,"HttpClient", lambda: stub)
    ring = Polygon(box(120,-12,128,-5).exterior.coords,[box(121,-10,126,-6).exterior.coords])
    assert ring.envelope.intersects(box(122.51,-8.78,124.93,-7.46))
    assert not search_slc(ring, DATE, DATE)
    assert not search_slc(AOI,"2024-04-05","2024-06-10")
    assert [r[0] for r in stub.calls] == ["GET"]


def test_duplicate_asf_file_with_different_digest_fails_closed(monkeypatch):
    doc = fixture_asf()
    changed = copy.deepcopy(doc["features"][0]); changed["properties"]["md5sum"] = "0"*32
    doc["features"].append(changed)
    stub = SearchStub(doc)
    monkeypatch.setattr(open_slc,"HttpClient",lambda:stub)
    with pytest.raises(UserError,match="不一致"):
        search_slc(AOI,DATE,DATE)
    assert [m for m, _, _ in stub.calls] == ["GET"]


@pytest.mark.parametrize("field,value",[
    ("url",slc_ref(SINGLE).url+"?other=1"),("name",DUAL+".zip"),("transport","cdse-s3"),("asset","manifest"),
    ("bytes", True),("provider_md5", None),("identity",{"kind":SLC_PROOF,"product":SINGLE,"archive_etag":"W/"+TAG}),
])
def test_no_redirect_path_or_checksum_substitution(field,value):
    with pytest.raises(UserError):
        check_slc_target({**target(b"a"),field:value})


def tiff(sample_format=5, big=False):
    # Place the first IFD at the tail like both real sampled SAFE versions.
    offset = 1536
    data = (struct.pack("<2sHHHQ", b"II",43,8,0,offset) if big else struct.pack("<2sHI", b"II",42,offset)).ljust(offset,b"z")
    tags = [(256,4,8),(257,4,6),(258,3,32),(277,3,1),(339,3,sample_format)]
    data += struct.pack("<Q" if big else "<H",len(tags))
    for tag, dtype, val in tags:
        data += struct.pack("<HHQQ" if big else "<HHII",tag,dtype,1,val)
    return data + b"\0"*(8 if big else 4)


def archive_fixture(product=SINGLE, fault=None):
    ref=slc_ref(product)
    root=ET.Element("XFDU")
    ms=ET.SubElement(root,"metadataSection")
    def meta(label):
        o=ET.SubElement(ms,"metadataObject",ID=label)
        return ET.SubElement(ET.SubElement(ET.SubElement(o,"metadataWrap"),"xmlData"),label)
    p=meta("platform"); ET.SubElement(p,"familyName").text="SENTINEL-1"; ET.SubElement(p,"number").text="B" if fault=="satellite" else "A"
    m=ET.SubElement(ET.SubElement(ET.SubElement(p,"instrument"),"extension"),"instrumentMode"); ET.SubElement(m,"mode").text="IW"
    for i in range(1,4): ET.SubElement(m,"swath").text=f"IW{i}"
    p=meta("generalProductInformation"); ET.SubElement(p,"productType").text="GRD" if fault=="product_type" else "SLC"
    ET.SubElement(p,"productClass").text="S"
    ET.SubElement(p,"missionDataTakeID").text=str(int(ref.datatake,16))
    for pol in ref.polarisations: ET.SubElement(p,"transmitterReceiverPolarisation").text=pol
    p=meta("measurementOrbitReference"); ET.SubElement(p,"orbitNumber",type="start").text=str(ref.orbit)
    p=meta("acquisitionPeriod"); ET.SubElement(p,"startTime").text="2014-11-01T10:08:20Z" if fault=="time" else ref.start.isoformat()
    ET.SubElement(p,"stopTime").text=ref.end.isoformat()
    payload={}
    for i in range(1,4):
        for pol in ref.polarisations:
            stem=f"s1a-iw{i}-slc-{pol.lower()}-{ref.start:%Y%m%dt%H%M%S}-{ref.end:%Y%m%dt%H%M%S}-{ref.orbit:06d}-{ref.datatake.lower()}-001"
            payload[f"measurement/{stem}.tiff"]=tiff(1 if fault=="noncomplex" and i==3 else 5,big=(i==2))
            payload[f"annotation/{stem}.xml"]=b"<p/>"
            payload[f"annotation/calibration/calibration-{stem}.xml"]=b"<cal/>"
            if fault!="missing_noise" or i!=2:
                payload[f"annotation/calibration/noise-{stem}.xml"]=b"<noise/>"
    objects=ET.SubElement(root,"dataObjectSection")
    for name,data in payload.items():
        stream=ET.SubElement(ET.SubElement(objects,"dataObject"),"byteStream",size=str(len(data)))
        ET.SubElement(stream,"fileLocation",href="./"+name)
        ET.SubElement(stream,"checksum",checksumName="MD5").text="0"*32 if fault=="member_md5" else md5(data)
    payload["manifest.safe"]=ET.tostring(root)
    if fault == "xml_doctype": payload["manifest.safe"]=b"<!DOCTYPE XFDU>"+payload["manifest.safe"]
    if fault == "traversal": payload["x/../other.xml"]=b"x"
    if fault == "case_alias": payload["MANIFEST.SAFE"]=payload["manifest.safe"]
    if fault == "extra_measurement": payload["measurement/other.tiff"]=tiff()
    if fault == "undecodable_crc": payload["support/example.xsd"]=b"unsafe-ref-only"
    out=io.BytesIO()
    with zipfile.ZipFile(out,"w",zipfile.ZIP_STORED) as z:
        for name, data in payload.items(): z.writestr(product+".SAFE/"+name,data)
    data=out.getvalue()
    return data.replace(b"unsafe-ref-only",b"Unsafe-ref-only") if fault == "undecodable_crc" else data


@pytest.mark.parametrize("product,number",[(SINGLE,3),(DUAL,6)])
def test_archive_exact_safe_identity_crc_and_signed_tiff_directory(tmp_path,product,number):
    data=archive_fixture(product)
    f=tmp_path/"download.part"; f.write_bytes(data)
    checked=slc_archive.verify_slc_archive(f,target(data,product))
    assert checked["zip_files_crc_verified"]==number*4+1
    assert checked["declared_members_md5_verified"]==number*4
    assert len(checked["measurements"])==number and all(m["shape"]==[8,6] for m in checked["measurements"])


@pytest.mark.parametrize("fault",["time","satellite","product_type","noncomplex","missing_noise","member_md5","xml_doctype",
                                   "traversal","case_alias","extra_measurement","undecodable_crc"])
def test_safe_validation_rejects_bad_science_or_structure_even_with_transport_digest(tmp_path,fault):
    data=archive_fixture(fault=fault)
    f=tmp_path/"download.part"; f.write_bytes(data)
    with pytest.raises(UserError,match="成员校验"):
        slc_archive.verify_slc_archive(f,target(data))


class RangeStub:
    def __init__(self, data, fault=None):
        self.data,self.fault=data,fault
        self.lock=threading.Lock()
        self.calls=[]
        self.responses=[]

    def request(self,method,url,*,headers,stream,cancel,timeout,attempts=1):
        assert attempts == 1
        assert method=="GET" and url==slc_ref(SINGLE).url and headers["If-Match"]==TAG and stream and headers["Accept-Encoding"]=="identity"
        i,j=map(int,re.fullmatch(r"bytes=(\d+)-(\d+)",headers["Range"]).groups())
        with self.lock:
            self.calls.append((i,j))
        h={"ETag":TAG,"Content-Type":"binary/octet-stream","Content-Range":f"bytes {i}-{j}/{len(self.data)}","Content-Length":str(j-i+1)}
        body=self.data[i:j+1]
        code=206
        if self.fault=="truncate_first" and i==0:
            class Short(StubResponse):
                def iter_content(self, size):
                    yield self.content[:53]
                    raise requests.exceptions.ChunkedEncodingError("cut")
            resp=Short(code,headers=h,content=body)
        elif self.fault=="cancel":
            class UntilCancelled(StubResponse):
                def iter_content(self, size):
                    cancel.wait(2)
                    yield b"x"
            resp=UntilCancelled(code,headers=h,content=body)
        else:
            if self.fault=="etag": h["ETag"]='"'+'f'*32+'-10"'
            if self.fault=="status": code=200
            if self.fault=="length": h["Content-Length"]=str(j-i)
            if self.fault=="range": h["Content-Range"]=f"bytes {i}-{j}/{len(self.data)+1}"
            if self.fault=="412": code=412
            if self.fault=="overflow": body+=b"x"
            if self.fault=="always_short": body=b""
            resp=StubResponse(code,headers=h,content=body)
        with self.lock:
            self.responses.append(resp)
        return resp

    def close(self):
        pass


def test_segmented_transfer_retries_only_unfinished_bytes_and_reorders_correctly(monkeypatch,tmp_path):
    data=archive_fixture(); stub=RangeStub(data,"truncate_first")
    monkeypatch.setattr(slc_archive,"RANGE_BYTES",512)
    monkeypatch.setattr(slc_archive,"HttpClient",lambda:stub)
    path=tmp_path/"wire.part"
    proof=slc_archive.transfer_slc(target(data),path,None,lambda *_:None)
    assert proof["range_workers"]==4 and proof["transfer_attempts"]==proof["range_windows"]+1 and path.read_bytes()==data
    assert (0,511) in stub.calls and (53,511) in stub.calls and all(r.closed for r in stub.responses)


@pytest.mark.parametrize("fault",["etag","status","length","range","412","overflow","always_short","bad_digest","bad_archive"])
def test_full_path_no_failed_archive_receipt_or_partial(monkeypatch,tmp_path,fault):
    data=archive_fixture(fault="missing_noise" if fault=="bad_archive" else None)
    stub=RangeStub(data if fault!="bad_digest" else data[:-1]+b"x",fault)
    monkeypatch.setattr(slc_archive,"RANGE_BYTES",512)
    monkeypatch.setattr(slc_archive,"HttpClient",lambda:stub)
    item=target(data)
    plan={"folder_name":"result","source":"ASF","options":{"collection":open_slc.SLC_COLLECTION},"targets":[item]}
    with pytest.raises(UserError):
        raw_download.download_original(AOI,plan,tmp_path,None)
    assert not list((tmp_path/"result").glob("*.zip")) and not list((tmp_path/"result").glob("*.starmap.json"))
    assert not list((tmp_path/"result").glob("*.part")) and all(r.closed for r in stub.responses)


def test_cancel_joins_range_writers_and_discards_partial(monkeypatch,tmp_path):
    stop=threading.Event(); data=archive_fixture(); stub=RangeStub(data,"cancel")
    monkeypatch.setattr(slc_archive,"RANGE_BYTES",512)
    monkeypatch.setattr(slc_archive,"HttpClient",lambda:stub)
    job=threading.Timer(.1,stop.set); job.start()
    try:
        plan={"folder_name":"result","source":"ASF","options":{"collection":open_slc.SLC_COLLECTION},"targets":[target(data)]}
        with pytest.raises(Cancelled):
            raw_download.download_original(AOI,plan,tmp_path,None,cancel=stop)
    finally:
        job.cancel(); job.join()
    assert stub.calls and all(r.closed for r in stub.responses)
    assert not list((tmp_path/"result").glob("*.zip")) and not list((tmp_path/"result").glob("*.part"))


def test_success_is_original_filename_and_skip_must_recheck_verified_asf_digest(monkeypatch,tmp_path):
    data=archive_fixture(); stub=RangeStub(data)
    monkeypatch.setattr(slc_archive,"HttpClient",lambda:stub)
    plan={"folder_name":"result","source":"ASF","options":{"collection":open_slc.SLC_COLLECTION},"targets":[target(data)]}
    doc=raw_download.download_original(AOI,plan,tmp_path,None)
    archive=Path(doc["files"][0]["path"]); receipt=archive.with_suffix(".starmap.json")
    assert archive.name==SINGLE+".zip" and archive.read_bytes()==data
    calls=len(stub.calls)
    assert raw_download.download_original(AOI,plan,tmp_path,None)["files"][0]["status"]=="skipped"
    changed=data[:31]+bytes([data[31]^1])+data[32:]; archive.write_bytes(changed)
    meta=json.loads(receipt.read_bytes()); meta["sha256"]=hashlib.sha256(changed).hexdigest(); receipt.write_text(json.dumps(meta),encoding="utf-8")
    with pytest.raises(UserError,match="MD5"):
        raw_download.download_original(AOI,plan,tmp_path,None)
    assert len(stub.calls)==calls and archive.read_bytes()==changed
