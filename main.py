import asyncio
import base64
import hashlib
import httpx
import io
import json
import os
import random
import re
import requests
import shutil
import sqlite3
import struct
import sys
import time
import zlib
import subprocess
from abc import ABC, abstractmethod
from collections.abc import Callable, Coroutine
from datetime import datetime
from pathlib import Path
from typing import Any, TypeVar

import UnityPy
import xml.etree.ElementTree as ET
from PIL import ImageFile
from typing_extensions import ParamSpec, override

# 依赖的辅助模块在仓库根目录，不依赖 Windows 的 D:\Data 路径
from _download_github_directory import DownloadTask
from _swf_handle import (
	AMF3Reader,
	extract_binary_data,
	extract_swf_data,
)

from pytz import timezone

ImageFile.LOAD_TRUNCATED_IMAGES = True

# === 路径配置（让脚本在 GitHub Actions/Linux 下也能跑）===
REPO_ROOT = Path(__file__).resolve().parent
OUTPUT_ROOT = Path(os.getenv("OUTPUT_ROOT", REPO_ROOT / "generated_files")).resolve()
PLUGIN_BASE_DIR = Path(os.getenv("PLUGIN_BASE_DIR", OUTPUT_ROOT / "plugins")).resolve()
data_path = Path(os.getenv("DATA_PATH", PLUGIN_BASE_DIR / "新数据")).resolve()
FLASH_DIR = Path(os.getenv("FLASH_DIR", OUTPUT_ROOT / "flash")).resolve()
FFDEC_JAR_PATH = Path(os.getenv("FFDEC_JAR_PATH", PLUGIN_BASE_DIR / "ffdec_18.0.0" / "ffdec.jar")).resolve()

LOCAL_BASE = str(PLUGIN_BASE_DIR)
IMG_LOCAL_BASE = LOCAL_BASE

OUTPUT_ROOT.mkdir(parents=True, exist_ok=True)
PLUGIN_BASE_DIR.mkdir(parents=True, exist_ok=True)
data_path.mkdir(parents=True, exist_ok=True)
FLASH_DIR.mkdir(parents=True, exist_ok=True)

# 确保能导入仓库根目录下的辅助模块
if str(REPO_ROOT) not in sys.path:
	sys.path.insert(0, str(REPO_ROOT))

""" Flash新数据 """

FLASH_VERSION_CHECK_URL = "https://seer.61.com/version/zzz_config.txt"

T_ParamSpec = ParamSpec('T_ParamSpec')
T_Retval = TypeVar('T_Retval')


def run_async_in_sync(
        async_func: Callable[T_ParamSpec, Coroutine[Any, Any, T_Retval]],
        *args: T_ParamSpec.args,
        **kwargs: T_ParamSpec.kwargs,
) -> T_Retval:
    """在同步函数中运行异步函数"""
    loop = asyncio.get_event_loop()
    return loop.run_until_complete(async_func(*args, **kwargs))


def handle_item_xml_info(data: list[dict]) -> dict:
    result = {}
    for obj in data:
        cat_obj = obj["catObj"]
        cat_id = cat_obj["ID"]
        if cat_id not in result:
            cat_obj["item"] = []
            result[cat_id] = cat_obj

        item: Any = obj["itemObj"]
        item["CatID"] = cat_id
        cat_obj["item"].append(item)

    return {"root": add_at_prefix_to_keys({"items": list(result.values())})}


def handle_gold_product_xml_info(data: list[dict]) -> dict:
    def _delete_class(obj: dict) -> dict:
        obj.pop("__class__")
        return obj

    return {"root": add_at_prefix_to_keys({"item": [_delete_class(obj) for obj in data]})}


def handle_skill_xml_info(data: list[dict]) -> dict:
    return {"root": add_at_prefix_to_keys({"item": data})}


AMF3_DATA_HANDLERS = {
    'com.robot.core.config.xml.ItemXMLInfo_xmlClass': handle_item_xml_info,
    'com.robot.core.config.xml.GoldProductXMLInfo_xmlClass': handle_gold_product_xml_info,
    'com.robot.core.config.xml.SkillXMLInfo_xmlClass': handle_skill_xml_info,
}

T = TypeVar('T', bound=dict[str, Any] | list[Any] | Any)


def add_at_prefix_to_keys(data: T) -> T:
    """为字典及嵌套字典中所有的 key 添加@前缀，除非值是列表（但是为列表中的字典添加@前缀）"""
    if isinstance(data, dict):
        result = {}
        for key, value in data.items():
            # 为 key 添加@前缀
            new_key = f"@{key}"

            if isinstance(value, list):
                # 如果值是列表，递归处理列表中的每个元素
                result[key] = [add_at_prefix_to_keys(item) for item in value]
            elif isinstance(value, dict):
                # 如果值是字典，递归处理
                result[new_key] = add_at_prefix_to_keys(value)
            else:
                # 其他类型直接赋值
                result[new_key] = value
        return result  # type: ignore
    elif isinstance(data, list):
        # 如果是列表，递归处理每个元素
        return [add_at_prefix_to_keys(item) for item in data]  # type: ignore
    else:
        # 其他类型直接返回
        return data


def dict_to_xml(data: dict) -> str:
    import xmltodict
    return xmltodict.unparse(
        data,
        pretty=True,
        full_document=False
    )


class Platform(ABC):
    VERSION_FILE_NAME = ".version"

    def __init__(self, work_dir: Path) -> None:
        super().__init__()
        self.work_dir = work_dir
        self.version_file_path = work_dir / self.VERSION_FILE_NAME
        self.work_dir.mkdir(parents=True, exist_ok=True)

    @abstractmethod
    def get_remote_version(self) -> str:
        pass

    @abstractmethod
    def get_configs(self) -> None:
        pass

    def get_local_version(self) -> str:
        if not self.version_file_path.exists():
            raise FileNotFoundError(f"{self.version_file_path} 不存在")
        return self.version_file_path.read_text().strip()

    def save_remote_version(self) -> None:
        self.version_file_path.write_text(self.get_remote_version())

    def check_update(self) -> bool:
        try:
            local_version = self.get_local_version()
        except FileNotFoundError:
            return True
        return local_version != self.get_remote_version()

def get_file_hash(data: bytes) -> str:
	return hashlib.sha256(data).hexdigest()

class Flash(Platform):
    def _download_swf_with_retry(self, url: str, max_retries: int = 4) -> bytes:
        """下载 SWF（网络抖动时做有限重试，避免 CI 因瞬时断流失败）。"""
        last_error: Exception | None = None
        for attempt in range(1, max_retries + 1):
            try:
                response = httpx.get(
                    url=url,
                    params={"t": random.uniform(0.01, 0.09)},
                    timeout=30.0,
                )
                response.raise_for_status()
                return response.content
            except httpx.HTTPError as exc:
                last_error = exc
                if attempt == max_retries:
                    break
                # 线性退避，给对端连接恢复时间
                time.sleep(attempt * 1.5)
        raise RuntimeError(f"下载失败（已重试 {max_retries} 次）：{url}") from last_error

    @staticmethod
    def extract_configs_from_swf(swf: bytes) -> dict[str, bytes]:
        decompressed = zlib.decompress(swf[7:])
        swf_data = extract_swf_data(decompressed)
        return extract_binary_data(swf_data)

    def _get_coredll_swf(self) -> bytes:
        return self._download_swf_with_retry("https://seer.61.com/dll/RobotCoreDLL.swf")

    def _get_prexml_swf(self) -> bytes:
        return self._download_swf_with_retry("https://seer.61.com/resource/xml/prexml.swf")

    @override
    def get_remote_version(self) -> str:
        coredll_swf = self._get_coredll_swf()
        prexml_swf = self._get_prexml_swf()
        file_hashs = frozenset(
            (
                get_file_hash(coredll_swf),
                get_file_hash(prexml_swf)
            )
        )
        return hashlib.sha256(str(file_hashs).encode()).hexdigest()

    def get_coredll_configs(self) -> None:
        import re

        swf = self._get_coredll_swf()
        swf_configs = self.extract_configs_from_swf(swf)
        for key, value in swf_configs.items():
            if value[:2] == b'\x78\xda':
                print(f"识别到压缩数据 {key}，解压中...")
                value = zlib.decompress(value)
                value = AMF3Reader(value).read_object()
                if handler := AMF3_DATA_HANDLERS.get(key):
                    value = handler(value)
                value = dict_to_xml(value)
                value = value.encode("utf-8")
            filename = re.sub(
                r'(_?(xmlclass|xmlcls)|com.robot.core.)', '', key, flags=re.IGNORECASE
            )
            filename = filename.strip('_')
            Path(f"{self.work_dir}/{filename}.xml").write_bytes(value)
    
    def get_prexml_configs(self) -> None:
        import zipfile
        import io

        swf = self._get_prexml_swf()
        prexml_dir = Path(self.work_dir) / "prexml"
        prexml_dir.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(io.BytesIO(swf)) as zip_file:
            for file_info in zip_file.infolist():
                xml_data = zip_file.read(file_info)
                filename = prexml_dir / file_info.filename
                filename.write_bytes(xml_data)

    @override
    def get_configs(self) -> None:
        self.get_coredll_configs()
        self.get_prexml_configs()


async def download_data_async(
        tasks: list[DownloadTask],
        output_dir: Path = Path("."),
        max_concurrency: int = 20,
        max_retries: int = 2,
        **client_kwargs: Any,
) -> None:
    async with (
        asyncio.Semaphore(max_concurrency),
        httpx.AsyncClient(**client_kwargs) as client,
    ):
        for url, filename in tasks:
            file_path = output_dir / filename
            file_path.parent.mkdir(parents=True, exist_ok=True)
            attempt = 0
            backoff_seconds = 0.5
            while True:
                try:
                    response = await client.get(url)
                    response.raise_for_status()
                    file_path.write_bytes(response.content)
                    break
                except httpx.HTTPStatusError as e:
                    print(f"{url} 下载失败，状态码：{e.response.status_code}")
                    break
                except httpx.HTTPError as e:
                    attempt += 1
                    if attempt > max_retries:
                        raise e
                    await asyncio.sleep(backoff_seconds)
                    backoff_seconds *= 2

    print(f"下载完成：{output_dir}, 共下载 {len(tasks)} 个文件")



""" Unity新数据 """

# === 可配置参数 ===
REMOTE_BASE = "https://newseer.61.com/Assets/StandaloneWindows64/ConfigPackage/"
PACKAGE_NAME = "ConfigPackage"
LOCAL_BASE = str(PLUGIN_BASE_DIR)
BUNDLE_DIR = os.path.join(LOCAL_BASE, "pgame_configs_bytes")
EXPORT_DIR = os.path.join(LOCAL_BASE, "新数据")
TARGET_TEXTASSET_NAMES = [
    "moves",
    "buff",
    "effectIcon",
    "effectDes",
    "effectbuff",
    "effectag",
    "effectInfo",
    "petbook",
    "skill_effect",
    "move_stones",
    "monsters",
    "addmoves",
    "awakendetail",
    "mintmark",
    "skillTypes",
    "sp_hide_moves",
    "petEffectIcon",
    "pvp_ban",
    "pvp_ban_expert",
    "pvp_vote",
    "pet_skin",
    "gems",
    "itemsOptimizeCat",
    "itemsTip",
]  # 只导出这些 TextAsset 名（导出为 moves.bytes 等）

# === 可配置参数 ===
IMG_REMOTE_BASE = "https://newseer.61.com/Assets/StandaloneWindows64/DefaultPackage/"
IMG_PACKAGE_NAME = "DefaultPackage"
IMG_LOCAL_BASE = LOCAL_BASE
IMG_BUNDLE_DIR = os.path.join(LOCAL_BASE, "assetbundles")
IMG_EXPORT_DIR = os.path.join(LOCAL_BASE, "新数据")

# === 简单的 BytesReader（小端，字符串长度用 uint16）===
class BytesReader:
    def __init__(self, data: bytes):
        self.f = io.BytesIO(data)

    def _read(self, n: int) -> bytes:
        b = self.f.read(n)
        if len(b) != n:
            raise EOFError("Unexpected EOF")
        return b

    def boolean(self) -> bool:
        return struct.unpack("<B", self._read(1))[0] != 0

    def ReadBoolean(self) -> bool:
        return self.boolean()

    def read_bool(self) -> bool:
        return self.boolean()

    def byte(self) -> int:
        return struct.unpack("<b", self._read(1))[0]

    def ubyte(self) -> int:
        return struct.unpack("<B", self._read(1))[0]

    def ushort(self) -> int:
        return struct.unpack("<H", self._read(2))[0]

    def short(self) -> int:
        return struct.unpack("<h", self._read(2))[0]

    def uint(self) -> int:
        return struct.unpack("<I", self._read(4))[0]

    def int(self) -> int:
        return struct.unpack("<i", self._read(4))[0]

    def ReadSignedInt(self) -> int:
        return self.int()

    def read_i32(self) -> int:
        return self.int()

    def read_u16(self) -> int:
        return self.ushort()

    def ulong(self) -> int:
        # TS 里 long 为 8 字节整型；Python 用有符号 64 位读取
        return struct.unpack("<q", self._read(8))[0]

    def text(self) -> str:
        ln = self.ushort()
        if ln == 0:
            return ""
        raw = self._read(ln)
        return raw.decode("utf-8", errors="surrogateescape")

    def ReadUTFBytesWithLength(self) -> str:
        return self.text()

    def ReadUTFBytesWithLength_exact(self) -> str:
        # 兼容调用方可能期望的同名方法（未使用）
        return self.text()

    def read_utf(self, length: int) -> str:
        if length == 0:
            return ""
        raw = self._read(length)
        return raw.decode("utf-8", errors="surrogateescape")

# === 解析 PackageManifest（与 TS 的 YooManifestParser 对齐）===
def parse_package_manifest(buf: bytes):
    r = BytesReader(buf)
    r.uint()  # skip FileVersion 前的占位
    file_version = r.text()
    enable_addressable = r.boolean()
    location_to_lower = r.boolean()
    include_asset_guid = r.boolean()
    output_name_type = r.int()
    package_name = r.text()
    package_version = r.text()

    asset_count = r.int()
    assets = []  # { AssetPath:str, BundleID:int, DependIDs:list[int] }
    for _ in range(asset_count):
        asset_path = r.text()
        bundle_id = r.int()
        dep_count = r.ushort()
        deps = [r.int() for _ in range(dep_count)]
        assets.append({
            "AssetPath": asset_path,
            "BundleID": bundle_id,
            "DependIDs": deps,
        })

    bundle_count = r.int()
    bundles = []  # index 即 BundleID；{ BundleName, UnityCRC, FileHash, FileCRC, FileSize, IsRawFile, LoadMethod, ReferenceIDs }
    for _ in range(bundle_count):
        bundles.append({
            "BundleName": r.text(),
            "UnityCRC": r.uint(),
            "FileHash": r.text(),
            "FileCRC": r.text(),
            "FileSize": r.ulong(),
            "IsRawFile": r.boolean(),
            "LoadMethod": r.byte(),
            "ReferenceIDs": [r.int() for _ in range(r.ushort())],
        })

    return {
        "FileVersion": file_version,
        "PackageName": package_name,
        "PackageVersion": package_version,
        "PackageAssetInfos": assets,
        "BundleList": bundles,
    }

def get_remote_version() -> str:
    url = REMOTE_BASE + f"PackageManifest_{PACKAGE_NAME}.version?t={int(time.time() * 1000)}"
    for attempt in range(1, 5):
        try:
            res = requests.get(url, timeout=20)
            res.raise_for_status()
            return res.text.strip()
        except requests.RequestException:
            if attempt == 4:
                raise
            time.sleep(attempt * 1.5)

def img_get_remote_version() -> str:
    url = IMG_REMOTE_BASE + f"PackageManifest_{IMG_PACKAGE_NAME}.version?t={int(time.time() * 1000)}"
    for attempt in range(1, 5):
        try:
            res = requests.get(url, timeout=20)
            res.raise_for_status()
            return res.text.strip()
        except requests.RequestException:
            if attempt == 4:
                raise
            time.sleep(attempt * 1.5)

def get_remote_manifest_bytes(version: str) -> bytes:
    url = REMOTE_BASE + f"PackageManifest_{PACKAGE_NAME}_{version}.bytes"
    for attempt in range(1, 5):
        try:
            res = requests.get(url, timeout=30)
            res.raise_for_status()
            return res.content
        except requests.RequestException:
            if attempt == 4:
                raise
            time.sleep(attempt * 1.5)

def img_get_remote_manifest_bytes(version: str) -> bytes:
    url = IMG_REMOTE_BASE + f"PackageManifest_{IMG_PACKAGE_NAME}_{version}.bytes"
    for attempt in range(1, 5):
        try:
            res = requests.get(url, timeout=30)
            res.raise_for_status()
            return res.content
        except requests.RequestException:
            if attempt == 4:
                raise
            time.sleep(attempt * 1.5)

def resolve_bundles_for_targets(manifest: dict, targets: list[str]) -> set[int]:
    # 目标名是 TextAsset 的 m_Name（导出时命名为 name.bytes）
    # 经验：AssetPath 通常包含 TextAsset 名；这里做宽松匹配：尾段或等于
    target_set = set(targets)
    bundle_ids = set()
    for a in manifest["PackageAssetInfos"]:
        asset_path = a["AssetPath"]
        name = os.path.splitext(os.path.basename(asset_path))[0]
        # 常见情况：AssetPath 末尾名 == TextAsset 名
        if name in target_set or asset_path.endswith(tuple("/" + t for t in target_set)):
            bundle_ids.add(a["BundleID"])
    return bundle_ids

def download_bundle(hash_str: str) -> bytes:
    # 远程文件按 FileHash 命名（与 TS 逻辑一致）
    url = REMOTE_BASE + hash_str
    with requests.get(url, stream=True, timeout=60) as r:
        r.raise_for_status()
        return r.content

def img_download_bundle(hash_str: str) -> bytes:
    # 远程文件按 FileHash 命名（与 TS 逻辑一致）
    url = IMG_REMOTE_BASE + hash_str
    with requests.get(url, stream=True, timeout=60) as r:
        r.raise_for_status()
        return r.content

def ensure_dir(path: str):
    os.makedirs(path, exist_ok=True)

def save_bundle(data: bytes, bundle_name: str):
    ensure_dir(BUNDLE_DIR)
    # 文件名可用 BundleName 或附加 .bundle；UnityPy 目录加载均可识别
    out_path = os.path.join(BUNDLE_DIR, f"{bundle_name}.bundle")
    with open(out_path, "wb") as f:
        f.write(data)

def img_save_bundle(data: bytes, bundle_name: str):
    ensure_dir(IMG_BUNDLE_DIR)
    # 文件名可用 BundleName 或附加 .bundle；UnityPy 目录加载均可识别
    out_path = os.path.join(IMG_BUNDLE_DIR, f"{bundle_name}.bundle")
    with open(out_path, "wb") as f:
        f.write(data)

def export_selected_textassets(names: list[str]):
    ensure_dir(EXPORT_DIR)
    env = UnityPy.load(BUNDLE_DIR)
    wanted = set(names)
    for obj in env.objects:
        if obj.type.name != "TextAsset":
            continue
        data = obj.read()
        if data.m_Name in wanted:
            out_path = os.path.join(EXPORT_DIR, f"{data.m_Name}.bytes")
            ensure_dir(os.path.dirname(out_path))
            with open(out_path, "wb") as f:
                f.write(data.m_Script.encode("utf-8", "surrogateescape"))
            print(f"导出: {out_path}")

def safe_filename(name: str) -> str:
    # 只保留字母、数字、下划线和点，其他替换为下划线
    return re.sub(r'[\\/:*?"<>|]', '_', name)

def export_all_png(path: str):
    ensure_dir(path)
    env = UnityPy.load(IMG_BUNDLE_DIR)
    for obj in env.objects:
        if obj.type.name != "Texture2D":
            continue
        tex = obj.read()
        img = tex.image
        if img is None:
            continue
        filename = safe_filename(tex.m_Name) + ".png"
        out_path = os.path.join(path, filename)
        ensure_dir(os.path.dirname(out_path))
        if not os.path.exists(out_path):
            img.save(out_path)
            print(f"导出: {out_path}")
    shutil.rmtree(IMG_BUNDLE_DIR)

def export_all_png_force(path: str):
    ensure_dir(path)
    env = UnityPy.load(IMG_BUNDLE_DIR)
    for obj in env.objects:
        if obj.type.name != "Texture2D":
            continue
        tex = obj.read()
        img = tex.image
        if img is None:
            continue
        filename = safe_filename(tex.m_Name) + ".png"
        out_path = os.path.join(path, filename)
        ensure_dir(os.path.dirname(out_path))
        img.save(out_path)
        print(f"导出: {out_path}")
    shutil.rmtree(IMG_BUNDLE_DIR)

def export_all_png_type(path: str):
    ensure_dir(path)
    env = UnityPy.load(IMG_BUNDLE_DIR)
    for obj in env.objects:
        if obj.type.name != "Sprite":
            continue
        tex = obj.read()
        img = tex.image
        if img is None:
            continue
        filename = safe_filename(tex.m_Name) + ".png"
        out_path = os.path.join(path, filename)
        ensure_dir(os.path.dirname(out_path))
        img.save(out_path)
        print(f"导出: {out_path}")
    shutil.rmtree(IMG_BUNDLE_DIR)

def write_json(output_json_path: str, result: dict) -> None:
    os.makedirs(os.path.dirname(output_json_path), exist_ok=True)
    with open(output_json_path, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False)
    print(f"导出: {output_json_path}")

def parse_and_dump_awakendetail(input_bytes_path, output_json_path):
    # 结构（根据样例十六进制推断）：
    # root_exists: bool
    # count: int32
    # repeat count times:
    #   text: string (u16 length + utf8 bytes)  ← 富文本，如 <indent=...>
    #   a:int32, b:int32, c:int32, d:int32, id:int32  ← 仅保留 id
    with open(input_bytes_path, "rb") as f:
        data = f.read()

    r = BytesReader(data)
    result = {"root": {"Task": []}}

    # 检查根布尔标志
    if not r.read_bool():
        pass
    else:
        if r.read_bool():
            n = r.ReadSignedInt()
            task = []
            for _ in range(n):
                temp = {}
                if r.read_bool():
                    advances = {}
                    if r.read_bool():
                        adveffect = {}
                        adveffect["Des"] = r.ReadUTFBytesWithLength()
                        adveffect["ID"] = r.ReadSignedInt()
                        advances["AdvEffect"] = adveffect
                    advances["AdvType"] = r.ReadSignedInt()
                    advances["MonsterId"] = r.ReadSignedInt()
                    if r.read_bool():
                        race = {}
                        if r.read_bool():
                            a = r.ReadSignedInt()
                            race["NewRace"] = [r.ReadSignedInt() for _ in range(a)]
                        if r.read_bool():
                            b = r.ReadSignedInt()
                            race["OldRace"] = [r.ReadSignedInt() for _ in range(b)]
                        advances["Race"] = race
                    if r.read_bool():
                        exmove = {}
                        exmove["ExtraMoves"] = r.ReadSignedInt()
                        advances["exMove"] = exmove
                    if r.read_bool():
                        spmove = {}
                        if r.read_bool():
                            c = r.ReadSignedInt()
                            spmove["SpvMoves"] = [r.ReadSignedInt() for _ in range(c)]
                        advances["spMove"] = spmove
                    temp["Advances"] = advances
                temp["ID"] = r.ReadSignedInt()
                temp["NewEffID"] = r.ReadSignedInt()
                temp["OldEffID"] = r.ReadSignedInt()
                task.append(temp)
            result["root"]["Task"] = task
    write_json(output_json_path, result)

def parse_and_dump_monsters(input_bytes_path, output_json_path):
    # 结构（根据样例十六进制推断）：
    # root_exists: bool
    # count: int32
    # repeat count times:
    #   text: string (u16 length + utf8 bytes)  ← 富文本，如 <indent=...>
    #   a:int32, b:int32, c:int32, d:int32, id:int32  ← 仅保留 id
    with open(input_bytes_path, "rb") as f:
        data = f.read()

    r = BytesReader(data)
    result = {"Monsters": {"Monster": []}}

    # 检查根布尔标志
    if not r.read_bool():
        pass
    else:
        if r.read_bool():
            n = r.ReadSignedInt()
            monster = []
            for _ in range(n):
                temp = {}
                temp["Atk"] = r.ReadSignedInt()
                temp["CharacterAttrParam"] = r.ReadSignedInt()
                temp["Combo"] = r.ReadSignedInt()
                temp["Def"] = r.ReadSignedInt()
                temp["DefName"] = r.ReadUTFBytesWithLength()
                temp["EvolvFlag"] = r.ReadSignedInt()
                temp["EvolvesTo"] = r.ReadSignedInt()
                temp["EvolvingLv"] = r.ReadSignedInt()
                if r.read_bool():
                    extramoves = {}
                    if r.read_bool():
                        a = r.ReadSignedInt()
                        advmove = []
                        for _ in range(a):
                            a_t = {}
                            a_t["ID"] = r.ReadSignedInt()
                            a_t["Rec"] = r.ReadSignedInt()
                            a_t["Tag"] = r.ReadSignedInt()
                            a_t["tag"] = r.ReadSignedInt()
                            advmove.append(a_t)
                        extramoves["AdvMove"] = advmove
                    if r.read_bool():
                        b = r.ReadSignedInt()
                        move = []
                        for _ in range(b):
                            m_t = {}
                            m_t["ID"] = r.ReadSignedInt()
                            m_t["LearningLv"] = r.ReadSignedInt()
                            m_t["Rec"] = r.ReadSignedInt()
                            m_t["Tag"] = r.ReadSignedInt()
                            move.append(m_t)
                        extramoves["Move"] = move
                    if r.read_bool():
                        c = r.ReadSignedInt()
                        spmove = []
                        for _ in range(c):
                            s_t = {}
                            s_t["ID"] = r.ReadSignedInt()
                            s_t["Rec"] = r.ReadSignedInt()
                            s_t["Tag"] = r.ReadSignedInt()
                            s_t["tag"] = r.ReadSignedInt()
                            spmove.append(s_t)
                        extramoves["SpMove"] = spmove
                    temp["ExtraMoves"] = extramoves
                temp["FreeForbidden"] = r.ReadSignedInt()
                temp["Gender"] = r.ReadSignedInt()
                temp["HP"] = r.ReadSignedInt()
                temp["ID"] = r.ReadSignedInt()
                if r.read_bool():
                    learnablemoves = {}
                    if r.read_bool():
                        a = r.ReadSignedInt()
                        advmove = []
                        for _ in range(a):
                            a_t = {}
                            a_t["ID"] = r.ReadSignedInt()
                            a_t["Rec"] = r.ReadSignedInt()
                            a_t["Tag"] = r.ReadSignedInt()
                            a_t["tag"] = r.ReadSignedInt()
                            advmove.append(a_t)
                        learnablemoves["AdvMove"] = advmove
                    if r.read_bool():
                        b = r.ReadSignedInt()
                        move = []
                        for _ in range(b):
                            m_t = {}
                            m_t["ID"] = r.ReadSignedInt()
                            m_t["LearningLv"] = r.ReadSignedInt()
                            m_t["Rec"] = r.ReadSignedInt()
                            m_t["Tag"] = r.ReadSignedInt()
                            move.append(m_t)
                        learnablemoves["Move"] = move
                    if r.read_bool():
                        c = r.ReadSignedInt()
                        spmove = []
                        for _ in range(c):
                            s_t = {}
                            s_t["ID"] = r.ReadSignedInt()
                            s_t["Rec"] = r.ReadSignedInt()
                            s_t["Tag"] = r.ReadSignedInt()
                            s_t["tag"] = r.ReadSignedInt()
                            spmove.append(s_t)
                        learnablemoves["SpMove"] = spmove
                    temp["LearnableMoves"] = learnablemoves
                if r.read_bool():
                    move = {}
                    move["ID"] = r.ReadSignedInt()
                    move["LearningLv"] = r.ReadSignedInt()
                    move["Rec"] = r.ReadSignedInt()
                    move["Tag"] = r.ReadSignedInt()
                    temp["Move"] = move
                temp["PetClass"] = r.ReadSignedInt()
                temp["RealId"] = r.ReadSignedInt()
                if r.read_bool():
                    showextramoves = {}
                    if r.read_bool():
                        a = r.ReadSignedInt()
                        advmove = []
                        for _ in range(a):
                            a_t = {}
                            a_t["ID"] = r.ReadSignedInt()
                            a_t["Rec"] = r.ReadSignedInt()
                            a_t["Tag"] = r.ReadSignedInt()
                            a_t["tag"] = r.ReadSignedInt()
                            advmove.append(a_t)
                        showextramoves["AdvMove"] = advmove
                    if r.read_bool():
                        b = r.ReadSignedInt()
                        move = []
                        for _ in range(b):
                            m_t = {}
                            m_t["ID"] = r.ReadSignedInt()
                            m_t["LearningLv"] = r.ReadSignedInt()
                            m_t["Rec"] = r.ReadSignedInt()
                            m_t["Tag"] = r.ReadSignedInt()
                            move.append(m_t)
                        showextramoves["Move"] = move
                    if r.read_bool():
                        c = r.ReadSignedInt()
                        spmove = []
                        for _ in range(c):
                            s_t = {}
                            s_t["ID"] = r.ReadSignedInt()
                            s_t["Rec"] = r.ReadSignedInt()
                            s_t["Tag"] = r.ReadSignedInt()
                            s_t["tag"] = r.ReadSignedInt()
                            spmove.append(s_t)
                        showextramoves["SpMove"] = spmove
                    temp["ShowExtraMoves"] = showextramoves
                temp["SpAtk"] = r.ReadSignedInt()
                temp["SpDef"] = r.ReadSignedInt()
                if r.read_bool():
                    spextramoves = {}
                    if r.read_bool():
                        a = r.ReadSignedInt()
                        advmove = []
                        for _ in range(a):
                            a_t = {}
                            a_t["ID"] = r.ReadSignedInt()
                            a_t["Rec"] = r.ReadSignedInt()
                            a_t["Tag"] = r.ReadSignedInt()
                            a_t["tag"] = r.ReadSignedInt()
                            advmove.append(a_t)
                        spextramoves["AdvMove"] = advmove
                    if r.read_bool():
                        b = r.ReadSignedInt()
                        move = []
                        for _ in range(b):
                            m_t = {}
                            m_t["ID"] = r.ReadSignedInt()
                            m_t["LearningLv"] = r.ReadSignedInt()
                            m_t["Rec"] = r.ReadSignedInt()
                            m_t["Tag"] = r.ReadSignedInt()
                            move.append(m_t)
                        spextramoves["Move"] = move
                    if r.read_bool():
                        c = r.ReadSignedInt()
                        spmove = []
                        for _ in range(c):
                            s_t = {}
                            s_t["ID"] = r.ReadSignedInt()
                            s_t["Rec"] = r.ReadSignedInt()
                            s_t["Tag"] = r.ReadSignedInt()
                            s_t["tag"] = r.ReadSignedInt()
                            spmove.append(s_t)
                        spextramoves["SpMove"] = spmove
                    temp["SpExtraMoves"] = spextramoves
                temp["Spd"] = r.ReadSignedInt()
                temp["Support"] = r.ReadSignedInt()
                temp["Transform"] = r.ReadSignedInt()
                temp["Type"] = r.ReadSignedInt()
                temp["Vip"] = r.ReadSignedInt()
                temp["isFlyPet"] = r.ReadSignedInt()
                temp["isRidePet"] = r.ReadSignedInt()
                monster.append(temp)
            result["Monsters"]["Monster"] = monster
    write_json(output_json_path, result)

def parse_and_dump_mintmark(input_bytes_path, output_json_path):
    # 结构（根据样例十六进制推断）：
    # root_exists: bool
    # count: int32
    # repeat count times:
    #   text: string (u16 length + utf8 bytes)  ← 富文本，如 <indent=...>
    #   a:int32, b:int32, c:int32, d:int32, id:int32  ← 仅保留 id
    with open(input_bytes_path, "rb") as f:
        data = f.read()

    r = BytesReader(data)
    result = {"MintMarks": {"MintMark": [], "MintMarkClass": []}}

    # 检查根布尔标志
    if not r.read_bool():
        pass
    else:
        if r.read_bool():
            n = r.ReadSignedInt()
            mtmk = []
            for _ in range(n):
                temp = {}
                if r.read_bool():
                    a = r.ReadSignedInt()
                    temp["Arg"] = [r.ReadSignedInt() for _ in range(a)]
                if r.read_bool():
                    b = r.ReadSignedInt()
                    temp["BaseAttriValue"] = [r.ReadSignedInt() for _ in range(b)]
                temp["Connect"] = r.ReadSignedInt()
                temp["Des"] = r.ReadUTFBytesWithLength()
                temp["EffectDes"] = r.ReadUTFBytesWithLength()
                if r.read_bool():
                    c = r.ReadSignedInt()
                    temp["ExtraAttriValue"] = [r.ReadSignedInt() for _ in range(c)]
                temp["Grade"] = r.ReadSignedInt()
                temp["Hide"] = r.ReadSignedInt()
                temp["ID"] = r.ReadSignedInt()
                temp["Level"] = r.ReadSignedInt()
                temp["Max"] = r.ReadSignedInt()
                if r.read_bool():
                    d = r.ReadSignedInt()
                    temp["MaxAttriValue"] = [r.ReadSignedInt() for _ in range(d)]
                temp["MintmarkClass"] = r.ReadSignedInt()
                if r.read_bool():
                    e = r.ReadSignedInt()
                    temp["MonsterID"] = [r.ReadSignedInt() for _ in range(e)]
                if r.read_bool():
                    f = r.ReadSignedInt()
                    temp["MoveID"] = [r.ReadSignedInt() for _ in range(f)]
                temp["Quality"] = r.ReadSignedInt()
                temp["Rare"] = r.ReadSignedInt()
                temp["Rarity"] = r.ReadSignedInt()
                temp["TotalConsume"] = r.ReadSignedInt()
                temp["Type"] = r.ReadSignedInt()
                mtmk.append(temp)
            result["MintMarks"]["MintMark"] = mtmk
        if r.read_bool():
            n = r.ReadSignedInt()
            mtmkc = []
            for _ in range(n):
                temp = {}
                temp["ClassName"] = r.ReadUTFBytesWithLength()
                temp["ID"] = r.ReadSignedInt()
                mtmkc.append(temp)
            result["MintMarks"]["MintMarkClass"] = mtmkc
    write_json(output_json_path, result)

def parse_and_dump_pet_effect_icon(input_bytes_path, output_json_path):
    # 结构（根据样例十六进制推断）：
    # root_exists: bool
    # count: int32
    # repeat count times:
    #   text: string (u16 length + utf8 bytes)  ← 富文本，如 <indent=...>
    #   a:int32, b:int32, c:int32, d:int32, id:int32  ← 仅保留 id
    with open(input_bytes_path, "rb") as f:
        data = f.read()

    r = BytesReader(data)
    result = {"data": []}

    if not r.read_bool():
        write_json(output_json_path, result)
        return

    count = r.read_i32()
    for _ in range(count):
        result["data"].append({
            "Desc": r.ReadUTFBytesWithLength(),
            "affectedBoss": r.read_i32(),
            "effecticonid": r.read_i32(),
            "id": r.read_i32(),
            "isAdv": r.read_i32(),
            "petid": r.read_i32(),
        })

    write_json(output_json_path, result)

def parse_and_dump_effect_icon(input_bytes_path, output_json_path):
    # 读取原始二进制
    with open(input_bytes_path, "rb") as f:
        data = f.read()

    r = BytesReader(data)
    result = {"root": {"effect": []}}

    # 检查根布尔标志
    if not r.read_bool():
        # 写出空结果
        write_json(output_json_path, result)
        return

    # 检查effect数组存在标志
    if not r.read_bool():
        # 写出空结果
        write_json(output_json_path, result)
        return

    # 读取数组数量
    count = r.read_i32()

    # 循环读取效果图标项（严格按照解析顺序）
    for _ in range(count):
        # 按照解析顺序读取所有字段
        item_id = r.read_i32()  # Id
        analyze = r.ReadUTFBytesWithLength()
        args = r.ReadUTFBytesWithLength()
        come = r.ReadUTFBytesWithLength()

        # 处理可选的des字符串数组
        des = []
        if r.read_bool():
            des_count = r.read_i32()
            des = [r.ReadUTFBytesWithLength() for _ in range(des_count)]

        effect_id = r.read_i32()  # effectId
        icon_id = r.read_i32()  # iconId
        intensify = r.read_i32()  # intensify
        is_adv = r.read_i32()  # isAdv

        # 处理可选的kind整数数组
        kind = []
        if r.read_bool():
            kind_count = r.read_i32()
            kind = [r.read_i32() for _ in range(kind_count)]

        label = r.read_i32()  # label
        limited_type = r.read_i32()  # limitedType

        # 处理可选的petId整数数组
        pet_id = []
        if r.read_bool():
            pet_id_count = r.read_i32()
            pet_id = [r.read_i32() for _ in range(pet_id_count)]

        # 处理可选的specificId整数数组
        specific_id = []
        if r.read_bool():
            specific_id_count = r.read_i32()
            specific_id = [r.read_i32() for _ in range(specific_id_count)]

        # 处理可选的tag字符串数组
        tag = []
        if r.read_bool():
            tag_count = r.read_i32()
            tag = [r.ReadUTFBytesWithLength() for _ in range(tag_count)]

        target = r.read_i32()
        tips = r.ReadUTFBytesWithLength()
        to = r.read_i32()
        type_ = r.read_i32()

        effect_item = {
            "analyze": analyze,
            "args": args,
            "come": come,
            "des": des,
            "tag": tag,
            "tips": tips,
            "kind": kind,
            "pet_id": pet_id,
            "specific_id": specific_id,
            "effect_id": effect_id,
            "icon_id": icon_id,
            "id": item_id,
            "intensify": intensify,
            "is_adv": is_adv,
            "label": label,
            "limited_type": limited_type,
            "target": target,
            "to": to,
            "type": type_,
        }
        result["root"]["effect"].append(effect_item)

    write_json(output_json_path, result)

def parse_and_dump_effect_tag(input_bytes_path, output_json_path):
    # 读取二进制
    with open(input_bytes_path, "rb") as f:
        data = f.read()

    r = BytesReader(data)
    result = {"data": []}

    # 对齐解析流程：
    # bool hasData -> int count -> loop(count){ id:int, tag:string(u16+utf8) }
    if r.read_bool():
        count = r.read_i32()
        for _ in range(count):
            item = {
                "id": r.read_i32(),
                "tag": r.ReadUTFBytesWithLength(),
            }
            result["data"].append(item)

    write_json(output_json_path, result)

def parse_and_dump_effect_des(input_bytes_path, output_json_path):
    # 读取原始二进制
    with open(input_bytes_path, "rb") as f:
        data = f.read()

    r = BytesReader(data)
    result = {"root": {"item": []}}

    # 对齐 EffectDesParser.parse 的结构
    # 检查根布尔标志
    if not r.read_bool():
        # 如果根标志为假，返回空结构
        pass
    else:
        # 检查item数组存在标志
        if not r.read_bool():
            # 如果item数组标志为假，返回空结构
            pass
        else:
            # 读取数组数量
            count = r.read_i32()

            # 循环读取效果描述项（严格按照C#解析顺序）
            for _ in range(count):
                item = {
                    "desc": r.read_utf(r.read_u16()),  # desc: 先读长度再读字符串
                    "icon": r.read_i32(),  # icon
                    "id": r.read_i32(),  # id
                    "kind": r.read_i32(),  # kind
                    "kinddes": r.read_utf(r.read_u16()),  # kinddes: 先读长度再读字符串
                    "link": r.read_utf(r.read_u16()),  # link: 先读长度再读字符串
                    "monster": r.read_utf(r.read_u16()),  # monster: 先读长度再读字符串
                    "tab": r.read_i32(),  # tab
                }
                result["root"]["item"].append(item)

    write_json(output_json_path, result)

def parse_and_dump_effect_buff(input_bytes_path, output_json_path):
    # 读取原始二进制
    with open(input_bytes_path, "rb") as f:
        data = f.read()

    r = BytesReader(data)
    result = {"root": {"Buff": []}}

    # 对齐 EffectDesParser.parse 的结构
    # 检查根布尔标志
    if not r.read_bool():
        # 如果根标志为假，返回空结构
        pass
    else:
        # 检查item数组存在标志
        if not r.read_bool():
            # 如果item数组标志为假，返回空结构
            pass
        else:
            # 读取数组数量
            count = r.read_i32()

            # 循环读取效果描述项（严格按照C#解析顺序）
            for _ in range(count):
                item = {
                    "Desc": r.read_utf(r.read_u16()),
                    "ID": r.read_i32(),
                    "Kind": r.read_i32(),
                    "Name": r.read_utf(r.read_u16()),
                }
                result["root"]["Buff"].append(item)

    write_json(output_json_path, result)

def parse_and_dump_moves(input_bytes_path, output_json_path):
    # 读取原始二进制
    with open(input_bytes_path, "rb") as f:
        data = f.read()

    r = BytesReader(data)
    result = {"root": None}

    # 对齐 MovesParser.parse 的结构
    # root: _MovesTbl | None
    if r.ReadBoolean():  # 是否存在 MovesTbl
        moves_tbl = {"moves": None}

        if r.ReadBoolean():  # 是否存在 Moves
            moves = {"move": [], "text": ""}

            # 是否存在 Move 数组
            if r.ReadBoolean():
                move_count = r.ReadSignedInt()
                for _ in range(move_count):
                    move_item = {
                        "accuracy": r.ReadSignedInt(),
                        "atk_num": r.ReadSignedInt(),
                        "atk_type": r.ReadSignedInt(),
                        "category": r.ReadSignedInt(),
                        "crit_rate": r.ReadSignedInt(),

                        "friend_side_effect": [],
                        "friend_side_effect_arg": [],

                        "id": 0,
                        "max_pp": 0,
                        "mon_id": 0,
                        "must_hit": 0,
                        "name": "",
                        "power": 0,
                        "priority": 0,

                        "side_effect": [],
                        "side_effect_arg": [],

                        "type": 0,
                        "info": "",
                        "ordinary": 0,
                    }

                    # 可选 friend_side_effect 数组
                    if r.ReadBoolean():
                        n = r.ReadSignedInt()
                        move_item["friend_side_effect"] = [r.ReadSignedInt() for _ in range(n)]

                    # 可选 friend_side_effect_arg 数组
                    if r.ReadBoolean():
                        n = r.ReadSignedInt()
                        move_item["friend_side_effect_arg"] = [r.ReadSignedInt() for _ in range(n)]

                    # 基础字段
                    move_item["id"] = r.ReadSignedInt()
                    move_item["max_pp"] = r.ReadSignedInt()
                    move_item["mon_id"] = r.ReadSignedInt()
                    move_item["must_hit"] = r.ReadSignedInt()
                    move_item["name"] = r.ReadUTFBytesWithLength()
                    move_item["power"] = r.ReadSignedInt()
                    move_item["priority"] = r.ReadSignedInt()

                    # 可选 side_effect 数组
                    if r.ReadBoolean():
                        n = r.ReadSignedInt()
                        move_item["side_effect"] = [r.ReadSignedInt() for _ in range(n)]

                    # 可选 side_effect_arg 数组
                    if r.ReadBoolean():
                        n = r.ReadSignedInt()
                        move_item["side_effect_arg"] = [r.ReadSignedInt() for _ in range(n)]

                    # 剩余字段
                    move_item["type"] = r.ReadSignedInt()
                    move_item["info"] = r.ReadUTFBytesWithLength()
                    move_item["ordinary"] = r.ReadSignedInt()

                    moves["move"].append(move_item)

            # 末尾附带的文本
            moves["text"] = r.ReadUTFBytesWithLength()
            moves_tbl["moves"] = moves

        result["root"] = moves_tbl

    write_json(output_json_path, result)

def parse_and_dump_effect_info(input_bytes_path, output_json_path):
    # 读取原始二进制
    with open(input_bytes_path, "rb") as f:
        data = f.read()

    r = BytesReader(data)
    result = {"root": {"effect": [], "param_type": []}}

    # 检查根布尔标志
    if not r.read_bool():
        # 如果根标志为 False，返回空结构
        pass
    else:
        # 读取 Effect 数组
        if r.read_bool():
            effect_count = r.read_i32()
            for _ in range(effect_count):
                # 按照 C# 解析顺序读取字段
                analyze = r.ReadUTFBytesWithLength()
                args_num = r.read_i32()  # argsNum
                effect_id = r.read_i32()  # id
                info = r.ReadUTFBytesWithLength()
                key = r.ReadUTFBytesWithLength()

                # 处理可选的 param 数组
                param = []
                if r.read_bool():
                    param_count = r.read_i32()
                    param = [r.read_i32() for _ in range(param_count)]

                type_ = r.read_i32()  # type
                effect_item = {
                    "analyze": analyze,
                    "info": info,
                    "param": param,
                    "args_num": args_num,
                    "id": effect_id,
                    "key": key,
                    "type": type_,
                }
                result["root"]["effect"].append(effect_item)

        # 读取 ParamType 数组
        if r.read_bool():
            param_type_count = r.read_i32()
            for _ in range(param_type_count):
                param_type_item = {
                    "id": r.read_i32(),
                    "params": r.ReadUTFBytesWithLength(),
                }
                result["root"]["param_type"].append(param_type_item)

    write_json(output_json_path, result)

def parse_and_dump_skill_effect(input_bytes_path, output_json_path):
    # 结构（根据样例十六进制推断）：
    # root_exists: bool
    # count: int32
    # repeat count times:
    #   text: string (u16 length + utf8 bytes)  ← 富文本，如 <indent=...>
    #   a:int32, b:int32, c:int32, d:int32, id:int32  ← 仅保留 id
    with open(input_bytes_path, "rb") as f:
        data = f.read()

    r = BytesReader(data)
    result = {"data": []}

    # 检查根布尔标志
    if not r.read_bool():
        # 如果根标志为 False，返回空结构
        pass
    else:
        count = r.ReadSignedInt()
        for _ in range(count):
            item = {
                "Bosseffective": r.ReadSignedInt(),
                "argsNum": r.ReadSignedInt(),
                "formattingAdjustment": r.ReadUTFBytesWithLength(),
                "id": r.ReadSignedInt(),
                "ifTextItalic": r.ReadUTFBytesWithLength(),
                "info": r.ReadUTFBytesWithLength(),
                "isif": r.ReadSignedInt(),
                "tagA": r.ReadUTFBytesWithLength(),
                "tagAboss": r.ReadSignedInt(),
                "tagB": r.ReadUTFBytesWithLength(),
                "tagBboss": r.ReadSignedInt(),
                "tagC": r.ReadUTFBytesWithLength(),
                "tagCboss": r.ReadSignedInt(),
            }
            result["data"].append(item)

    write_json(output_json_path, result)

def parse_and_dump_skilltypes(input_bytes_path, output_json_path):
    # 结构（根据样例十六进制推断）：
    # root_exists: bool
    # count: int32
    # repeat count times:
    #   text: string (u16 length + utf8 bytes)  ← 富文本，如 <indent=...>
    #   a:int32, b:int32, c:int32, d:int32, id:int32  ← 仅保留 id
    with open(input_bytes_path, "rb") as f:
        data = f.read()

    r = BytesReader(data)
    result = {"root": []}

    # 检查根布尔标志
    if not r.read_bool():
        pass
    else:
        if r.read_bool():
            n = r.ReadSignedInt()
            item = []
            for _ in range(n):
                temp = {}
                temp["att"] = r.ReadUTFBytesWithLength()
                temp["cn"] = r.ReadUTFBytesWithLength()
                if r.read_bool():
                    a = r.ReadSignedInt()
                    temp["en"] = [r.ReadUTFBytesWithLength() for _ in range(a)]
                temp["id"] = r.ReadSignedInt()
                temp["is_dou"] = r.ReadSignedInt()
                item.append(temp)
            result["root"] = item
    write_json(output_json_path, result)

def parse_and_dump_sp_hide_moves(input_bytes_path, output_json_path):
    with open(input_bytes_path, "rb") as f:
        data = f.read()

    r = BytesReader(data)
    result = {"root": {"ShowMoves": [], "SpMoves": []}}

    # 检查根布尔标志
    if r.read_bool():
        if r.read_bool():
            n = r.ReadSignedInt()
            showmoves = []
            for _ in range(n):
                temp = {}
                temp["id"] = r.ReadSignedInt()
                temp["item"] = r.ReadSignedInt()
                temp["itemname"] = r.ReadUTFBytesWithLength()
                temp["itemnumber"] = r.ReadSignedInt()
                temp["monster"] = r.ReadSignedInt()
                temp["moves"] = r.ReadSignedInt()
                temp["movesname"] = r.ReadUTFBytesWithLength()
                temp["movetype"] = r.ReadSignedInt()
                showmoves.append(temp)
            result["root"]["ShowMoves"] = showmoves
        if r.read_bool():
            n = r.ReadSignedInt()
            spmoves = []
            for _ in range(n):
                temp = {}
                temp["id"] = r.ReadSignedInt()
                temp["item"] = r.ReadSignedInt()
                temp["itemname"] = r.ReadUTFBytesWithLength()
                temp["itemnumber"] = r.ReadSignedInt()
                temp["monster"] = r.ReadSignedInt()
                temp["moves"] = r.ReadSignedInt()
                temp["movesname"] = r.ReadUTFBytesWithLength()
                temp["movetype"] = r.ReadSignedInt()
                spmoves.append(temp)
            result["root"]["SpMoves"] = spmoves
    write_json(output_json_path, result)

def parse_and_dump_pvp_ban(input_bytes_path, output_json_path):
    # 结构（根据样例十六进制推断）：
    # root_exists: bool
    # count: int32
    # repeat count times:
    #   text: string (u16 length + utf8 bytes)  ← 富文本，如 <indent=...>
    #   a:int32, b:int32, c:int32, d:int32, id:int32  ← 仅保留 id
    with open(input_bytes_path, "rb") as f:
        data = f.read()

    r = BytesReader(data)
    result = {"root": []}

    if r.read_bool():
        n = r.ReadSignedInt()
        item = []
        for _ in range(n):
            temp = {}
            temp["id"] = r.ReadSignedInt()
            if r.read_bool():
                a = r.ReadSignedInt()
                temp["name"] = [r.ReadSignedInt() for _ in range(a)]
            temp["quantity"] = r.ReadSignedInt()
            temp["subkey"] = r.ReadSignedInt()
            temp["type"] = r.ReadSignedInt()
            item.append(temp)
        result["root"] = item
    write_json(output_json_path, result)

def parse_and_dump_pvp_ban_expert(input_bytes_path, output_json_path):
    # 结构（根据样例十六进制推断）：
    # root_exists: bool
    # count: int32
    # repeat count times:
    #   text: string (u16 length + utf8 bytes)  ← 富文本，如 <indent=...>
    #   a:int32, b:int32, c:int32, d:int32, id:int32  ← 仅保留 id
    with open(input_bytes_path, "rb") as f:
        data = f.read()

    r = BytesReader(data)
    result = {"root": []}

    if r.read_bool():
        data = []
        n = r.ReadSignedInt()
        for _ in range(n):
            temp = {}
            temp["id"] = r.ReadSignedInt()
            temp["name"] = r.ReadUTFBytesWithLength()
            temp["quantity"] = r.ReadSignedInt()
            temp["reward"] = r.ReadUTFBytesWithLength()
            temp["seasonopen"] = r.ReadSignedInt()
            temp["subkey_month"] = r.ReadSignedInt()
            temp["subkey_total"] = r.ReadSignedInt()
            temp["type"] = r.ReadSignedInt()
            data.append(temp)
        result["root"] = data
    write_json(output_json_path, result)

def parse_and_dump_pvp_vote(input_bytes_path, output_json_path):
    # 结构（根据样例十六进制推断）：
    # root_exists: bool
    # count: int32
    # repeat count times:
    #   text: string (u16 length + utf8 bytes)  ← 富文本，如 <indent=...>
    #   a:int32, b:int32, c:int32, d:int32, id:int32  ← 仅保留 id
    with open(input_bytes_path, "rb") as f:
        data = f.read()

    r = BytesReader(data)
    result = {"root": []}

    if r.read_bool():
        data = []
        n = r.ReadSignedInt()
        for _ in range(n):
            temp = {}
            temp["id"] = r.ReadSignedInt()
            temp["name"] = r.ReadUTFBytesWithLength()
            temp["number"] = r.ReadSignedInt()
            temp["oldresult"] = r.ReadUTFBytesWithLength()
            temp["ranklimit1"] = r.ReadSignedInt()
            temp["ranklimit2"] = r.ReadSignedInt()
            temp["result"] = r.ReadUTFBytesWithLength()
            temp["subkey"] = r.ReadSignedInt()
            temp["time1"] = r.ReadSignedInt()
            temp["time2"] = r.ReadSignedInt()
            temp["type"] = r.ReadSignedInt()
            data.append(temp)
        result["root"] = data
    write_json(output_json_path, result)

def parse_and_dump_pet_skin(input_bytes_path, output_json_path):
    # 结构（根据样例十六进制推断）：
    # root_exists: bool
    # count: int32
    # repeat count times:
    #   text: string (u16 length + utf8 bytes)  ← 富文本，如 <indent=...>
    #   a:int32, b:int32, c:int32, d:int32, id:int32  ← 仅保留 id
    with open(input_bytes_path, "rb") as f:
        data = f.read()

    r = BytesReader(data)
    result = {"root": []}

    if r.read_bool():
        if r.read_bool():
            n = r.ReadSignedInt()
            skin = []
            for _ in range(n):
                temp = {}
                temp["Go"] = r.ReadUTFBytesWithLength()
                temp["GoType"] = r.ReadUTFBytesWithLength()
                temp["ID"] = r.ReadSignedInt()
                temp["Jumptarget"] = r.ReadSignedInt()
                temp["MonID"] = r.ReadSignedInt()
                temp["Name"] = r.ReadUTFBytesWithLength()
                if r.read_bool():
                    nn = r.ReadSignedInt()
                    skinkind = []
                    for _ in range(nn):
                        t = {}
                        t["ID"] = r.ReadSignedInt()
                        t["LifeTime"] = r.ReadSignedInt()
                        t["SkinType"] = r.ReadSignedInt()
                        t["Type"] = r.ReadSignedInt()
                        t["Year"] = r.ReadSignedInt()
                        skinkind.append(t)
                    temp["SkinKind"] = skinkind
                temp["Type"] = r.ReadSignedInt()
                skin.append(temp)
            result["root"] = skin
    write_json(output_json_path, result)

def parse_and_dump_gem(input_bytes_path, output_json_path):
    with open(input_bytes_path, "rb") as f:
        data = f.read()

    r = BytesReader(data)
    result = {"gems": {"gem": []}}

    if r.read_bool():
        if r.read_bool():
            n = r.ReadSignedInt()
            gem = []
            for _ in range(n):
                temp = {}
                temp["category"] = r.ReadSignedInt()
                temp["decompose_prob"] = r.ReadSignedInt()
                temp["des"] = r.ReadUTFBytesWithLength()
                temp["equit_lv1_cnt1"] = r.ReadSignedInt()
                temp["gid"] = r.ReadSignedInt()
                temp["lv"] = r.ReadSignedInt()
                temp["name"] = r.ReadUTFBytesWithLength()
                skille = []
                if r.read_bool():
                    nn = r.ReadSignedInt()
                    for _ in range(nn):
                        t = {}
                        if r.read_bool():
                            t["effect_id"] = r.ReadSignedInt()
                            param = []
                            if r.read_bool():
                                a = r.ReadSignedInt()
                                param = [r.ReadSignedInt() for _ in range(a)]
                                t["param"] = param
                        skille.append({"effect": t})
                temp["skill_effects"] = skille
                temp["upgrade_gem_id"] = r.ReadSignedInt()
                gem.append(temp)
            result["gems"]["gem"] = gem
    write_json(output_json_path, result)

def parse_and_dump_item(input_bytes_path, output_json_path):
    with open(input_bytes_path, "rb") as f:
        data = f.read()
    r = BytesReader(data)
    result = {"root": []}
    if r.read_bool():
        n = r.ReadSignedInt()
        cats = []
        for _ in range(n):
            temp = {}
            temp["DbCatID"] = r.ReadSignedInt()
            temp["ID"] = r.ReadSignedInt()
            temp["Max"] = r.ReadSignedInt()
            temp["Name"] = r.ReadUTFBytesWithLength()
            temp["url"] = r.ReadUTFBytesWithLength()
            cats.append(temp)
        result["root"] = cats
    write_json(output_json_path, result)

    ttnid = []
    ttn = []
    for i in result["root"]:
        ttnid.append(i["ID"])
        ttn.append("itemsOptimizeCatItems" + str(i["ID"]))
        
    manifest_bytes = get_remote_manifest_bytes(version1)
    manifest = parse_package_manifest(manifest_bytes)
    bundle_ids = resolve_bundles_for_targets(manifest, ttn)
    for bid in bundle_ids:
        b = manifest["BundleList"][bid]
        data = download_bundle(b["FileHash"])
        save_bundle(data, b["BundleName"])
    export_selected_textassets(ttn)

    for i in ttnid:
        with open(data_path / f"itemsOptimizeCatItems{i}.bytes", "rb") as f:
            data = f.read()

        r = BytesReader(data)
        resul = {"root": []}

        if r.read_bool():
            n = r.ReadSignedInt()
            items = []
            for _ in range(n):
                temp = {}
                match i:
                    case 0:
                        temp["Bean"] = r.ReadSignedInt()
                        temp["Hide"] = r.ReadSignedInt()
                        temp["ID"] = r.ReadSignedInt()
                        temp["LifeTime"] = r.ReadSignedInt()
                        temp["Max"] = r.ReadSignedInt()
                        temp["Name"] = r.ReadUTFBytesWithLength()
                        temp["Price"] = r.ReadSignedInt()
                        temp["Rarity"] = r.ReadSignedInt()
                        temp["Sort"] = r.ReadSignedInt()
                        temp["UseMax"] = r.ReadSignedInt()
                        temp["catID"] = r.ReadSignedInt()
                        temp["purpose"] = r.ReadSignedInt()
                        temp["wd"] = r.ReadSignedInt()
                    case 1:
                        temp["Bean"] = r.ReadSignedInt()
                        temp["Hide"] = r.ReadSignedInt()
                        temp["ID"] = r.ReadSignedInt()
                        temp["LifeTime"] = r.ReadSignedInt()
                        temp["Max"] = r.ReadSignedInt()
                        temp["Name"] = r.ReadUTFBytesWithLength()
                        temp["Price"] = r.ReadSignedInt()
                        temp["RepairPrice"] = r.ReadSignedInt()
                        temp["Sort"] = r.ReadSignedInt()
                        temp["UseMax"] = r.ReadSignedInt()
                        temp["VipOnly"] = r.ReadSignedInt()
                        temp["actionDir"] = r.ReadSignedInt()
                        temp["catID"] = r.ReadSignedInt()
                        temp["isSpecial"] = r.ReadSignedInt()
                        temp["purpose"] = r.ReadSignedInt()
                        temp["speed"] = r.ReadSignedInt()
                        temp["type"] = r.ReadUTFBytesWithLength()
                        temp["wd"] = r.ReadSignedInt()
                    case 2:
                        temp["Color"] = r.ReadUTFBytesWithLength()
                        temp["ID"] = r.ReadSignedInt()
                        temp["Max"] = r.ReadSignedInt()
                        temp["Name"] = r.ReadUTFBytesWithLength()
                        temp["Price"] = r.ReadSignedInt()
                        temp["Texture"] = r.ReadSignedInt()
                        temp["catID"] = r.ReadSignedInt()
                    case 3:
                        temp["Bean"] = r.ReadSignedInt()
                        temp["EvRemove"] = r.ReadSignedInt()
                        temp["Hide"] = r.ReadSignedInt()
                        temp["ID"] = r.ReadSignedInt()
                        temp["IncreMonLvTo"] = r.ReadSignedInt()
                        temp["ItemType"] = r.ReadSignedInt()
                        temp["LimitPetClass"] = r.ReadUTFBytesWithLength()
                        temp["Max"] = r.ReadSignedInt()
                        temp["Name"] = r.ReadUTFBytesWithLength()
                        temp["PP"] = r.ReadSignedInt()
                        temp["Price"] = r.ReadSignedInt()
                        temp["Rarity"] = r.ReadSignedInt()
                        temp["Sort"] = r.ReadSignedInt()
                        temp["UseMax"] = r.ReadSignedInt()
                        temp["VipOnly"] = r.ReadSignedInt()
                        temp["catID"] = r.ReadSignedInt()
                        temp["purpose"] = r.ReadSignedInt()
                        temp["wd"] = r.ReadSignedInt()
                    case 4:
                        temp["Bean"] = r.ReadSignedInt()
                        temp["Hide"] = r.ReadSignedInt()
                        temp["ID"] = r.ReadSignedInt()
                        temp["Max"] = r.ReadSignedInt()
                        temp["Name"] = r.ReadUTFBytesWithLength()
                        temp["Price"] = r.ReadSignedInt()
                        temp["Rarity"] = r.ReadSignedInt()
                        temp["Sort"] = r.ReadSignedInt()
                        temp["UseMax"] = r.ReadSignedInt()
                        temp["VipOnly"] = r.ReadSignedInt()
                        temp["catID"] = r.ReadSignedInt()
                        temp["purpose"] = r.ReadSignedInt()
                        temp["wd"] = r.ReadSignedInt()
                    case 5:
                        temp["Bean"] = r.ReadSignedInt()
                        temp["Hide"] = r.ReadSignedInt()
                        temp["ID"] = r.ReadSignedInt()
                        temp["Max"] = r.ReadSignedInt()
                        temp["Name"] = r.ReadUTFBytesWithLength()
                        temp["Price"] = r.ReadSignedInt()
                        temp["Sort"] = r.ReadSignedInt()
                        temp["VipOnly"] = r.ReadSignedInt()
                        temp["catID"] = r.ReadSignedInt()
                        temp["purpose"] = r.ReadSignedInt()
                        temp["wd"] = r.ReadSignedInt()
                    case 6:
                        temp["ID"] = r.ReadSignedInt()
                        temp["Max"] = r.ReadSignedInt()
                        temp["Name"] = r.ReadUTFBytesWithLength()
                        temp["catID"] = r.ReadSignedInt()
                        temp["wd"] = r.ReadSignedInt()
                    case 7:
                        temp["Bean"] = r.ReadSignedInt()
                        temp["Hide"] = r.ReadSignedInt()
                        temp["ID"] = r.ReadSignedInt()
                        temp["Max"] = r.ReadSignedInt()
                        temp["Name"] = r.ReadUTFBytesWithLength()
                        temp["Sort"] = r.ReadSignedInt()
                        temp["catID"] = r.ReadSignedInt()
                        temp["hideNum"] = r.ReadSignedInt()
                        temp["purpose"] = r.ReadSignedInt()
                        temp["wd"] = r.ReadSignedInt()
                    case 8:
                        temp["ID"] = r.ReadSignedInt()
                        temp["Max"] = r.ReadSignedInt()
                        temp["Name"] = r.ReadUTFBytesWithLength()
                        temp["catID"] = r.ReadSignedInt()
                    case 9:
                        temp["ID"] = r.ReadSignedInt()
                        temp["Max"] = r.ReadSignedInt()
                        temp["Name"] = r.ReadUTFBytesWithLength()
                        temp["catID"] = r.ReadSignedInt()
                    case 10:
                        temp["Bean"] = r.ReadSignedInt()
                        temp["Hide"] = r.ReadSignedInt()
                        temp["ID"] = r.ReadSignedInt()
                        temp["Max"] = r.ReadSignedInt()
                        temp["Name"] = r.ReadUTFBytesWithLength()
                        temp["Sort"] = r.ReadSignedInt()
                        temp["UseMax"] = r.ReadSignedInt()
                        temp["catID"] = r.ReadSignedInt()
                        temp["purpose"] = r.ReadSignedInt()
                        temp["wd"] = r.ReadSignedInt()
                    case 11:
                        temp["Bean"] = r.ReadSignedInt()
                        temp["ID"] = r.ReadSignedInt()
                        temp["Max"] = r.ReadSignedInt()
                        temp["Name"] = r.ReadUTFBytesWithLength()
                        temp["NeedLv"] = r.ReadSignedInt()
                        temp["Rank"] = r.ReadSignedInt()
                        temp["Rarity"] = r.ReadSignedInt()
                        temp["Sort"] = r.ReadSignedInt()
                        temp["Type"] = r.ReadSignedInt()
                        temp["catID"] = r.ReadSignedInt()
                        temp["purpose"] = r.ReadSignedInt()
                        temp["wd"] = r.ReadSignedInt()
                    case 12:
                        temp["ExchangeId"] = r.ReadSignedInt()
                        temp["Hide"] = r.ReadSignedInt()
                        temp["ID"] = r.ReadSignedInt()
                        temp["Max"] = r.ReadSignedInt()
                        temp["Name"] = r.ReadUTFBytesWithLength()
                        temp["Rarity"] = r.ReadSignedInt()
                        temp["Sort"] = r.ReadSignedInt()
                        temp["TargetId"] = r.ReadSignedInt()
                        temp["catID"] = r.ReadSignedInt()
                        temp["purpose"] = r.ReadSignedInt()
                        temp["wd"] = r.ReadSignedInt()
                    case 13:
                        temp["Bean"] = r.ReadSignedInt()
                        temp["Hide"] = r.ReadSignedInt()
                        temp["ID"] = r.ReadSignedInt()
                        temp["Max"] = r.ReadSignedInt()
                        temp["Name"] = r.ReadUTFBytesWithLength()
                        temp["Sort"] = r.ReadSignedInt()
                        temp["UseMax"] = r.ReadSignedInt()
                        temp["VipOnly"] = r.ReadSignedInt()
                        temp["catID"] = r.ReadSignedInt()
                        temp["isSpecial"] = r.ReadSignedInt()
                        temp["purpose"] = r.ReadSignedInt()
                        temp["speed"] = r.ReadSignedInt()
                        temp["type"] = r.ReadUTFBytesWithLength()
                        temp["wd"] = r.ReadSignedInt()
                    case 14:
                        temp["Bean"] = r.ReadSignedInt()
                        temp["Hide"] = r.ReadSignedInt()
                        temp["ID"] = r.ReadSignedInt()
                        temp["Max"] = r.ReadSignedInt()
                        temp["Name"] = r.ReadUTFBytesWithLength()
                        temp["Rarity"] = r.ReadSignedInt()
                        temp["Sort"] = r.ReadSignedInt()
                        temp["catID"] = r.ReadSignedInt()
                        temp["purpose"] = r.ReadSignedInt()
                        temp["wd"] = r.ReadSignedInt()
                    case 15:
                        temp["Bean"] = r.ReadSignedInt()
                        temp["Hide"] = r.ReadSignedInt()
                        temp["ID"] = r.ReadSignedInt()
                        temp["Max"] = r.ReadSignedInt()
                        temp["Name"] = r.ReadUTFBytesWithLength()
                        temp["Sort"] = r.ReadSignedInt()
                        temp["UseMax"] = r.ReadSignedInt()
                        temp["catID"] = r.ReadSignedInt()
                        temp["purpose"] = r.ReadSignedInt()
                        temp["wd"] = r.ReadSignedInt()
                    case 16:
                        temp["ID"] = r.ReadSignedInt()
                        temp["Max"] = r.ReadSignedInt()
                        temp["Name"] = r.ReadUTFBytesWithLength()
                        temp["catID"] = r.ReadSignedInt()
                    case 17:
                        temp["Bean"] = r.ReadSignedInt()
                        temp["ExchangeId"] = r.ReadSignedInt()
                        temp["ExchangeOutCnt"] = r.ReadUTFBytesWithLength()
                        temp["ExchangeOutId"] = r.ReadUTFBytesWithLength()
                        temp["ExchangeType"] = r.ReadSignedInt()
                        temp["Hide"] = r.ReadSignedInt()
                        temp["ID"] = r.ReadSignedInt()
                        temp["ItemType"] = r.ReadSignedInt()
                        temp["LifeTime"] = r.ReadSignedInt()
                        temp["Max"] = r.ReadSignedInt()
                        temp["Name"] = r.ReadUTFBytesWithLength()
                        temp["Price"] = r.ReadSignedInt()
                        temp["Rarity"] = r.ReadSignedInt()
                        temp["SkinId"] = r.ReadSignedInt()
                        temp["Sort"] = r.ReadSignedInt()
                        temp["TargetId"] = r.ReadSignedInt()
                        temp["UseEnd"] = r.ReadUTFBytesWithLength()
                        temp["catID"] = r.ReadSignedInt()
                        temp["hideNum"] = r.ReadSignedInt()
                        temp["purpose"] = r.ReadSignedInt()
                        temp["wd"] = r.ReadSignedInt()
                    case 18:
                        temp["Bean"] = r.ReadSignedInt()
                        temp["Hide"] = r.ReadSignedInt()
                        temp["ID"] = r.ReadSignedInt()
                        temp["Max"] = r.ReadSignedInt()
                        temp["Name"] = r.ReadUTFBytesWithLength()
                        temp["Rarity"] = r.ReadSignedInt()
                        temp["Sort"] = r.ReadSignedInt()
                        temp["catID"] = r.ReadSignedInt()
                        temp["purpose"] = r.ReadSignedInt()
                        temp["wd"] = r.ReadSignedInt()
                    case 19:
                        temp["ID"] = r.ReadSignedInt()
                        temp["Max"] = r.ReadSignedInt()
                        temp["Name"] = r.ReadUTFBytesWithLength()
                        temp["catID"] = r.ReadSignedInt()
                    case 20:
                        temp["ID"] = r.ReadSignedInt()
                        temp["Max"] = r.ReadSignedInt()
                        temp["Name"] = r.ReadUTFBytesWithLength()
                        temp["catID"] = r.ReadSignedInt()
                    case 21:
                        temp["ID"] = r.ReadSignedInt()
                        temp["Max"] = r.ReadSignedInt()
                        temp["Name"] = r.ReadUTFBytesWithLength()
                        temp["catID"] = r.ReadSignedInt()
                    case 22:
                        temp["ID"] = r.ReadSignedInt()
                        temp["Max"] = r.ReadSignedInt()
                        temp["Name"] = r.ReadUTFBytesWithLength()
                        temp["catID"] = r.ReadSignedInt()
                    case 23:
                        temp["Bean"] = r.ReadSignedInt()
                        temp["ExchangeOutCnt"] = r.ReadUTFBytesWithLength()
                        temp["ExchangeOutId"] = r.ReadUTFBytesWithLength()
                        temp["Hide"] = r.ReadSignedInt()
                        temp["ID"] = r.ReadSignedInt()
                        temp["LifeTime"] = r.ReadSignedInt()
                        temp["Max"] = r.ReadSignedInt()
                        temp["Name"] = r.ReadUTFBytesWithLength()
                        temp["Rarity"] = r.ReadSignedInt()
                        temp["Sort"] = r.ReadSignedInt()
                        temp["UseMax"] = r.ReadSignedInt()
                        temp["catID"] = r.ReadSignedInt()
                        temp["icon"] = r.ReadSignedInt()
                        temp["purpose"] = r.ReadSignedInt()
                        temp["wd"] = r.ReadSignedInt()
                    case 24:
                        temp["Hide"] = r.ReadSignedInt()
                        temp["ID"] = r.ReadSignedInt()
                        temp["Max"] = r.ReadSignedInt()
                        temp["Name"] = r.ReadUTFBytesWithLength()
                        temp["Sort"] = r.ReadSignedInt()
                        temp["catID"] = r.ReadSignedInt()
                        temp["purpose"] = r.ReadSignedInt()
                        temp["wd"] = r.ReadSignedInt()
                    case 25:
                        temp["Hide"] = r.ReadSignedInt()
                        temp["ID"] = r.ReadSignedInt()
                        temp["LifeTime"] = r.ReadSignedInt()
                        temp["Max"] = r.ReadSignedInt()
                        temp["Name"] = r.ReadUTFBytesWithLength()
                        temp["Rarity"] = r.ReadSignedInt()
                        temp["Sort"] = r.ReadSignedInt()
                        temp["UseMax"] = r.ReadSignedInt()
                        temp["catID"] = r.ReadSignedInt()
                        temp["wd"] = r.ReadSignedInt()
                    case 26:
                        temp["ID"] = r.ReadSignedInt()
                        temp["Max"] = r.ReadSignedInt()
                        temp["Name"] = r.ReadUTFBytesWithLength()
                        temp["Rarity"] = r.ReadSignedInt()
                        temp["Sort"] = r.ReadSignedInt()
                        temp["UseMax"] = r.ReadSignedInt()
                        temp["catID"] = r.ReadSignedInt()
                        temp["purpose"] = r.ReadSignedInt()
                        temp["wd"] = r.ReadSignedInt()
                items.append(temp)
            resul["root"] = items
        write_json(data_path / f"itemsOptimizeCatItems{i}.json", resul)
            
    with open(data_path / f"itemsTip.bytes", "rb") as f:
        data = f.read()
    r = BytesReader(data)
    resu = {"root": []}
    if r.read_bool():
        if r.read_bool():
            n = r.ReadSignedInt()
            item = []
            for _ in range(n):
                temp = {}
                temp["des"] = r.ReadUTFBytesWithLength()
                temp["id"] = r.ReadSignedInt()
                item.append(temp)
            resu["root"] = item
    write_json(data_path / f"itemsTip.json", resu)

def xml_to_json7(xml_file_path, output_json_path=None):
    """
    将Monster XML文件转换为指定格式的JSON
    
    参数:
        xml_file_path: XML文件路径
        output_json_path: 输出JSON文件路径，若为None则在原目录生成
    """
    # 解析XML文件
    tree = ET.parse(xml_file_path)
    root = tree.getroot()
    
    # 构建目标JSON结构
    result = {
        "root": {
            "Monster": []
        }
    }
    
    # 处理每个Monster元素
    for monster in root.findall('Monster'):
        # 基础属性字典
        monster_data = {}
        
        # 处理Monster的所有属性
        for attr in monster.attrib:
            # 尝试将数值类型的属性转换为整数
            try:
                monster_data[attr] = int(monster.attrib[attr])
            except (ValueError, TypeError):
                # 无法转换为整数的保持字符串类型
                monster_data[attr] = monster.attrib[attr]
        
        # 将处理好的怪物数据添加到结果中
        result['root']['Monster'].append(monster_data)
    
    # temp = {
    #     "ID": 4543,
    #     "DefName": "九青",
    #     "Type": "圣灵 地面",
    #     "Height": 162,
    #     "Weight": 56,
    #     "Features": "2023年9月巅峰圣战精灵……“有些约定，就是一开始便知道结局，也还是会义无反顾嘛……”九青看着手中的信封，呆呆的注视着望不到边际的北方。"
    # }
    # result['root']['Monster'].append(temp)
    
    # 如果未指定输出路径，在原XML目录生成
    if not output_json_path:
        xml_dir = os.path.dirname(xml_file_path)
        xml_filename = os.path.splitext(os.path.basename(xml_file_path))[0]
        output_json_path = os.path.join(xml_dir, f"{xml_filename}.json")
    
    # 保存为JSON文件
    with open(output_json_path, 'w', encoding='utf-8') as f:
        json.dump(result, f, ensure_ascii=False)
    
    print(f"转换完成，JSON文件已保存至: {output_json_path}")
    return result

def xml_to_json8(xml_file_path, output_json_path=None):
    """
    将Monster XML文件转换为指定格式的JSON
    
    参数:
        xml_file_path: XML文件路径
        output_json_path: 输出JSON文件路径，若为None则在原目录生成
    """
    # 解析XML文件
    tree = ET.parse(xml_file_path)
    root = tree.getroot()
    
    # 构建目标JSON结构
    result = {
        "MovesTbl": {
            "Moves": {
                "Move": []
            }
        }
    }
    
    # 处理每个Monster元素
    for monster in root.findall('item'):
        # 基础属性字典
        monster_data = {}
        
        # 处理Monster的所有属性
        for attr in monster.attrib:
            # 尝试将数值类型的属性转换为整数
            try:
                monster_data[attr] = int(monster.attrib[attr])
            except (ValueError, TypeError):
                # 无法转换为整数的保持字符串类型
                monster_data[attr] = monster.attrib[attr]
        
        # 将处理好的怪物数据添加到结果中
        result['MovesTbl']['Moves']['Move'].append(monster_data)
    
    # 如果未指定输出路径，在原XML目录生成
    if not output_json_path:
        xml_dir = os.path.dirname(xml_file_path)
        xml_filename = os.path.splitext(os.path.basename(xml_file_path))[0]
        output_json_path = os.path.join(xml_dir, f"{xml_filename}.json")
    
    # 保存为JSON文件
    with open(output_json_path, 'w', encoding='utf-8') as f:
        json.dump(result, f, ensure_ascii=False)
    
    print(f"转换完成，JSON文件已保存至: {output_json_path}")
    return result

def export_swf_to_svg(ffdec_jar_path, input_swf_path, export_type="frame"):
    root_dir_path = PLUGIN_BASE_DIR / "魂印"
    root_dir_path.mkdir(parents=True, exist_ok=True)
    if (root_dir_path / f"{input_swf_path}.svg").exists() or (root_dir_path / f"{input_swf_path}.png").exists():
        return
    # ========== 基础配置 ==========
    # 根目录（外层文件夹）
    root_dir = str(root_dir_path)
    # SWF专属目录（内层文件夹）
    swf_dir = os.path.join(root_dir, str(input_swf_path))
    # SWF文件路径
    swf_file_path = os.path.join(swf_dir, f"{input_swf_path}.swf")
    # 保存原始工作目录（修复缺失的original_cwd）
    original_cwd = os.getcwd()

    try:
        # CI 环境通常不会自带 ffdec；缺失时跳过导出，保证脚本其它数据仍能生成。
        if ffdec_jar_path and not os.path.exists(ffdec_jar_path):
            print(f"未找到 ffdec.jar：{ffdec_jar_path}，跳过魂印导出（{input_swf_path}）")
            return True

        # ========== 1. 创建目录 + 下载SWF ==========
        os.makedirs(swf_dir, exist_ok=True)

        # 下载SWF文件
        url = f'https://seer.61.com/resource/effectIcon/{input_swf_path}.swf'
        r = requests.get(url, timeout=10)  # 添加超时，避免卡死
        if r.status_code != 404:
            with open(swf_file_path, "wb") as file:
                file.write(r.content)
            print(f"✅ 成功下载SWF: {swf_file_path}")
        else:
            print(f"❌ 下载SWF失败，状态码: {r.status_code}")
            if not os.path.exists(swf_file_path):  # 本地无文件则直接返回
                return False

        # ========== 2. 切换目录 + 构建FFDec命令 ==========
        os.chdir(swf_dir)  # 切换到SWF专属目录

        # 构建基础命令
        command = ['java', '-jar', ffdec_jar_path]

        # 添加预选项（导出为SVG格式）
        if export_type == "frame":
            command.extend(['-format', 'frame:svg'])
        elif export_type == "sprite":
            try:
                command.extend(['-format', 'sprite:svg'])
            except Exception as e:
                command.extend(['-format', 'shape:svg'])

        # 添加导出指令（FFDec 18.x/24.x兼容格式）
        command.extend([
            '-export', export_type,
            swf_dir,  # 导出目录（SWF专属目录）
            f"{input_swf_path}.swf"  # SWF文件名（当前目录下）
        ])

        # ========== 3. 执行导出命令 ==========
        # 修复：延长超时时间（10秒太短），取消text=True避免编码错误
        result = subprocess.run(
            command,
            check=True,
            capture_output=True,
            timeout=10,
            encoding='gbk'  # 适配Windows中文输出
        )

        # ========== 4. 核心功能：查找最后文件夹 + 复制SVG ==========
        # 步骤1：遍历SWF目录下的所有子文件夹（精灵/帧文件夹）
        sub_folders = []
        for item in os.listdir(swf_dir):
            item_path = os.path.join(swf_dir, item)
            # 只筛选文件夹，且排除隐藏文件夹
            if os.path.isdir(item_path) and not item.startswith('.'):
                sub_folders.append(item_path)

        if not sub_folders:
            print(f"❌ 未找到任何子文件夹，无法复制SVG")
            return True  # 导出成功但无子文件夹，返回True

        # 步骤2：按文件夹名称排序（字符串排序），取最后一个
        # 排序规则：按名称从A-Z、0-9，取最后面的文件夹
        sub_folders.sort(key=lambda x: os.path.basename(x))
        last_folder = sub_folders[-1]
        last_folder_name = os.path.basename(last_folder)

        # 步骤3：查找该文件夹内的所有SVG文件
        svg_files = []
        for item in os.listdir(last_folder):
            if item.lower().endswith('.svg'):  # 忽略大小写
                svg_files.append(os.path.join(last_folder, item))

        if not svg_files:
            print(f"❌ 最后文件夹{last_folder_name}内无SVG文件")
            return True

        # 步骤4：复制SVG到外层文件夹（root_dir）
        for svg_file in svg_files:
            # 目标路径：外层文件夹 + 原SVG文件名（可自定义命名）
            target_svg_path = os.path.join(
                root_dir,  # 外层文件夹
                f"{input_swf_path}.svg"
            )
            # 复制文件（覆盖已存在的文件）
            shutil.copy2(svg_file, target_svg_path)
        
        # 步骤5：把有位图的svg改成png
        try:
            # 读取SVG文件内容（指定UTF-8编码，兼容中文文件名）
            with open(target_svg_path, 'r', encoding='utf-8') as f:
                svg_content = f.read()
            
            # 正则匹配SVG中内嵌的Base64位图（支持PNG/JPG/JPEG/GIF/BMP/WebP等常见格式）
            # 匹配规则：data:image/[格式];base64,[Base64编码内容]，忽略大小写
            pattern = r'data:image/(png|jpg|jpeg|gif|bmp|webp);base64,([^"\']+)'
            matches = re.findall(pattern, svg_content, re.IGNORECASE)
            
            if not matches:
                return
            
            # 遍历所有匹配的位图（一个SVG可能内嵌多个）
            for idx, (img_format, base64_data) in enumerate(matches, 1):
                try:
                    # 解码Base64数据（处理可能的空白字符）
                    base64_data = base64_data.strip()
                    img_data = base64.b64decode(base64_data)
                    
                    # 生成保存的图片文件名（避免重复，格式：SVG名_位图序号.格式）
                    img_filename = f"{input_swf_path}.{img_format.lower()}"
                    img_save_path = os.path.join(root_dir, img_filename)
                    
                    # 保存图片
                    with open(img_save_path, 'wb') as f:
                        f.write(img_data)
                    
                    os.remove(target_svg_path)
                
                    print(f"✅ {input_swf_path}：提取第{idx}个位图 → {img_save_path}")

                except base64.binascii.Error:
                    print(f"❌ {input_swf_path} 第{idx}个位图：Base64编码格式错误，提取失败")
                except Exception as e:
                    print(f"❌ {input_swf_path} 第{idx}个位图：提取失败，错误：{str(e)[:50]}...")
        
        except UnicodeDecodeError:
            # 尝试用GBK编码读取（兼容部分非UTF-8的SVG）
            try:
                with open(target_svg_path, 'r', encoding='gbk') as f:
                    svg_content = f.read()
                # 重新匹配位图
                matches = re.findall(pattern, svg_content, re.IGNORECASE)
                if matches:
                    # 复用上面的提取逻辑（简化版）
                    for idx, (img_format, base64_data) in enumerate(matches, 1):
                        try:
                            img_data = base64.b64decode(base64_data.strip())
                            img_filename = f"{input_swf_path}.{img_format.lower()}"
                            img_save_path = os.path.join(root_dir, img_filename)
                            with open(img_save_path, 'wb') as f:
                                f.write(img_data)
                            os.remove(target_svg_path)
                            print(f"✅ {input_swf_path}（GBK编码）：提取第{idx}个位图 → {img_save_path}")
                        except:
                            print(f"❌ {input_swf_path}（GBK编码）第{idx}个位图：提取失败")
            except:
                print(f"❌ {input_swf_path}：文件编码既非UTF-8也非GBK，无法读取（跳过）")
        except PermissionError:
            print(f"❌ {input_swf_path}：无读取权限（跳过）")
        except Exception as e:
            print(f"❌ {input_swf_path}：处理失败，错误：{str(e)[:50]}...")

    except Exception as e:
        print(f"✗ 发生意外错误: {type(e).__name__} - {str(e)}")
        return False
    finally:
        # 无论是否出错，都恢复原始工作目录
        os.chdir(original_cwd)

    return True





# 更新

ensure_dir(LOCAL_BASE)
version1 = get_remote_version()
with open(data_path/'version1.txt', "r", encoding="utf-8") as file:
    v1 = file.read()

ensure_dir(IMG_LOCAL_BASE)
version2 = img_get_remote_version()
with open(data_path/'version2.txt', "r", encoding="utf-8") as file:
    v2 = file.read()

platforms: list[tuple[str, Platform]] = [("flash", Flash(FLASH_DIR))]
for name, platform in platforms:
    version3 = platform.get_remote_version()
    platform.check_update()
    print(f"{platform.work_dir} 更新中...")
    platform.get_configs()
    platform.save_remote_version()
    time_str = datetime.now(timezone("Asia/Shanghai")).strftime("%Y-%m-%d %H:%M:%SUTC%z")

manifest_bytes = img_get_remote_manifest_bytes(version2)
manifest = parse_package_manifest(manifest_bytes)

manifest_bytes = get_remote_manifest_bytes(version1)
manifest = parse_package_manifest(manifest_bytes)
bundle_ids = resolve_bundles_for_targets(manifest, TARGET_TEXTASSET_NAMES)
for bid in bundle_ids:
    b = manifest["BundleList"][bid]
    data = download_bundle(b["FileHash"])
    save_bundle(data, b["BundleName"])

export_selected_textassets(TARGET_TEXTASSET_NAMES)


# """神谕/觉醒"""
# url1 = "http://seerh5.61.com/resource/config/xml/"+xml["pet_advance.json"]
# response = urllib.request.urlopen(url1)
# data = json.load(response)
parse_and_dump_awakendetail(
    (data_path / "awakendetail.bytes"),
    (data_path / "awakendetail.json"),
)
with open((data_path / "awakendetail.json"), 'r', encoding='utf-8') as f:
    data = json.load(f)
root = data["root"]
Task = root["Task"]


"""精灵"""
# url2 = "http://seerh5.61.com/resource/config/xml/"+xml["monsters.json"]
# response = urllib.request.urlopen(url2)
# data = json.load(response)
xml_path = str(FLASH_DIR / "config.xml.PetBookXMLInfo.xml")
json_path = (data_path / "petbook.json")
xml_to_json7(xml_path, json_path)
parse_and_dump_monsters(
    (data_path / "monsters.bytes"),
    (data_path / "monsters.json"),
)
with open((data_path / "monsters.json"), 'r', encoding='utf-8') as f:
    data = json.load(f)
Monsters = data["Monsters"]
Monster = Monsters["Monster"]
temp = {
    "Atk": 140,
    "CharacterAttrParam": 0,
    "Combo": 0,
    "Def": 110,
    "DefName": "九青",
    "EvolvFlag": 0,
    "EvolvesTo": 0,
    "EvolvingLv": 0,
    "ExtraMoves": {
        "Move": [
        {
            "ID": 36802,
            "LearningLv": 76,
            "Rec": 0,
            "Tag": 0
        }
        ]
    },
    "FreeForbidden": 1,
    "Gender": 2,
    "HP": 165,
    "ID": 4543,
    "LearnableMoves": {
        "Move": [
        {
            "ID": 10047,
            "LearningLv": 1,
            "Rec": 0,
            "Tag": 0
        },
        {
            "ID": 22660,
            "LearningLv": 5,
            "Rec": 0,
            "Tag": 0
        },
        {
            "ID": 14897,
            "LearningLv": 9,
            "Rec": 0,
            "Tag": 0
        },
        {
            "ID": 10313,
            "LearningLv": 13,
            "Rec": 0,
            "Tag": 0
        },
        {
            "ID": 14935,
            "LearningLv": 17,
            "Rec": 0,
            "Tag": 0
        },
        {
            "ID": 36798,
            "LearningLv": 21,
            "Rec": 0,
            "Tag": 0
        },
        {
            "ID": 22662,
            "LearningLv": 25,
            "Rec": 0,
            "Tag": 0
        },
        {
            "ID": 14898,
            "LearningLv": 29,
            "Rec": 0,
            "Tag": 0
        },
        {
            "ID": 22637,
            "LearningLv": 33,
            "Rec": 0,
            "Tag": 0
        },
        {
            "ID": 22053,
            "LearningLv": 37,
            "Rec": 0,
            "Tag": 0
        },
        {
            "ID": 24558,
            "LearningLv": 41,
            "Rec": 0,
            "Tag": 0
        },
        {
            "ID": 36799,
            "LearningLv": 45,
            "Rec": 0,
            "Tag": 0
        },
        {
            "ID": 28411,
            "LearningLv": 49,
            "Rec": 0,
            "Tag": 0
        },
        {
            "ID": 36800,
            "LearningLv": 53,
            "Rec": 0,
            "Tag": 0
        },
        {
            "ID": 28412,
            "LearningLv": 57,
            "Rec": 0,
            "Tag": 0
        },
        {
            "ID": 36801,
            "LearningLv": 61,
            "Rec": 0,
            "Tag": 0
        },
        ]
    },
    "SpAtk": 70,
    "SpDef": 110,
    "Spd": 130,
    "Type": 100,
}
Monster.append(temp)


"""刻印"""
# url3 = "http://seerh5.61.com/resource/config/xml/"+xml["mintmark.json"]
# response = urllib.request.urlopen(url3)
# data = json.load(response)
parse_and_dump_mintmark(
    (data_path / "mintmark.bytes"),
    (data_path / "mintmark.json"),
)
with open((data_path / "mintmark.json"), 'r', encoding='utf-8') as f:
    data = json.load(f)
MintMarks = data["MintMarks"]
MintMark = MintMarks["MintMark"]
MintmarkClass = MintMarks["MintMarkClass"]
Cid = []
Cname = []
Mid = []
Mname = []
Mdes = []
Mclass = []
Mvalue = []
for i in MintmarkClass:
    Cid.append(i["ID"])
    Cname.append(i["ClassName"][:-2])
for i in MintMark:
    Mid.append(str(i["ID"]))
    Mname.append(i["Des"])
    Mdes.append(i["Des"])
    try:
        Mclass.append(Cname[Cid.index(i["MintmarkClass"])])
    except:
        Mclass.append("专属刻印")
    try:
        if len(i["Arg"]) == 6:
            A,B,C,D,E,F = i["Arg"]
    except:
        try:
            A,B,C,D,E,F = i["MaxAttriValue"]
        except:
            A,B,C,D,E,F = [0,0,0,0,0,0]
        try:
            a,b,c,d,e,f = i["ExtraAttriValue"]
            A += a
            B += b
            C += c
            D += d
            E += e
            F += f
        except:
            pass
    Mvalue.append([str(A),str(B),str(C),str(D),str(E),str(F)])
    if '·' in i["Des"]:
        try:
            Mclass.append(Cname[Cid.index(i["MintmarkClass"])])
        except:
            Mclass.append("专属刻印")
        Mid.append(str(i["ID"]))
        Mname.append(i["Des"])
        Mdes.append(i["Des"].replace('·',''))
        Mvalue.append([str(A),str(B),str(C),str(D),str(E),str(F)])
    if '-' in i["Des"]:
        try:
            Mclass.append(Cname[Cid.index(i["MintmarkClass"])])
        except:
            Mclass.append("专属刻印")
        Mid.append(str(i["ID"]))
        Mname.append(i["Des"])
        Mdes.append(i["Des"].replace('-',''))
        Mvalue.append([str(A),str(B),str(C),str(D),str(E),str(F)])
    if i["Des"] != i["Des"].lower():
        try:
            Mclass.append(Cname[Cid.index(i["MintmarkClass"])])
        except:
            Mclass.append("专属刻印")
        Mid.append(str(i["ID"]))
        Mname.append(i["Des"])
        Mdes.append(i["Des"].lower())
        Mvalue.append([str(A),str(B),str(C),str(D),str(E),str(F)])
    if '-' in i["Des"] and i["Des"] != i["Des"].lower():
        try:
            Mclass.append(Cname[Cid.index(i["MintmarkClass"])])
        except:
            Mclass.append("专属刻印")
        Mid.append(str(i["ID"]))
        Mname.append(i["Des"])
        Mdes.append(i["Des"].replace('-','').lower())
        Mvalue.append([str(A),str(B),str(C),str(D),str(E),str(F)])


"""魂印"""
# url4 = "http://seerh5.61.com/resource/config/xml/"+xml["effectIcon.json"]
# response = urllib.request.urlopen(url4)
# data = json.load(response)
parse_and_dump_pet_effect_icon(
    (data_path / "petEffectIcon.bytes"),
    (data_path / "petEffectIcon.json"),
)
parse_and_dump_effect_icon(
    (data_path / "effectIcon.bytes"),
    (data_path / "effectIcon.json"),
)
with open((data_path / "effectIcon.json"), 'r', encoding='utf-8') as f:
    data = json.load(f)
root = data["root"]
effect = root["effect"]
for i in effect:    # 导出魂印图标
    export_swf_to_svg(
        ffdec_jar_path=str(FFDEC_JAR_PATH),
        input_swf_path=i["icon_id"],  # SWF文件名（数字）
        export_type="sprite"  # 导出精灵（frame=导出帧，sprite=导出精灵）
    )
parse_and_dump_effect_tag( # 魂印标签
    (data_path / "effectag.bytes"),
    (data_path / "effectag.json"),
)
"""魂印buff"""
# url5 = "http://seerh5.61.com/resource/config/xml/"+xml["effectbuff.json"]
# response = urllib.request.urlopen(url5)
# data = json.load(response)
parse_and_dump_effect_buff(
    (data_path / "effectbuff.bytes"),
    (data_path / "effectbuff.json"),
)
with open((data_path / "effectbuff.json"), 'r', encoding='utf-8') as f:
    data = json.load(f)
root = data["root"]
Buff = root["Buff"]
"""魂印标注"""
# url6 = "http://seerh5.61.com/resource/config/xml/"+xml["effectDes.json"]
# response = urllib.request.urlopen(url6)
# data = json.load(response)
parse_and_dump_effect_des(
    (data_path / "effectDes.bytes"),
    (data_path / "effectDes.json"),
)
with open((data_path / "effectDes.json"), 'r', encoding='utf-8') as f:
    data = json.load(f)
root = data["root"]
editem = root["item"]

e = []
eid = []
edesc = []
for i in effect:
    try:
        e.append([i["pet_id"][0],i["tips"]])
    except:
        pass
for i in e:
    eid.append(i[0])
    edesc.append(i[1])
buff = []
buff1 = []
buff2 = []
buff3 = []
for i in Buff:
    try:
        buff.append([i["Name"],i["Desc"],0])
    except:
        pass
for i in editem:
    try:
        buff.append([i["kinddes"],i["desc"],i["monster"]])
    except:
        buff.append([i["kinddes"],i["desc"],0])
for i in buff:
    buff1.append(i[0])
    buff2.append(i[1])
    buff3.append(i[2])


"""技能效果"""
# url7 = "http://seerh5.61.com/resource/config/xml/"+xml["moves.json"]
# response = urllib.request.urlopen(url7)
# data = json.load(response)
parse_and_dump_moves(
    (data_path / "moves.bytes"),
    (data_path / "moves_unity.json"),
)
with open((data_path / "moves_unity.json"), 'r', encoding='utf-8') as f:
    data = json.load(f)
MovesTbl = data["root"]
Moves = MovesTbl["moves"]
Moveu = Moves["move"]
xml_path = str(FLASH_DIR / "config.xml.SkillXMLInfo.xml")
json_path = (data_path / "moves.json")
xml_to_json8(xml_path, json_path)
with open((data_path / "moves.json"), 'r', encoding='utf-8') as f:
    data = json.load(f)
MovesTbl = data["MovesTbl"]
Moves = MovesTbl["Moves"]
Move = Moves["Move"]
temp = [
    {
        "ID": 36798,
        "Name": "生尘眷盼",
        "Category": 2,
        "Type": 100,
        "Power": 90,
        "MaxPP": 10,
        "Accuracy": 99,
        "Priority": 3,
        "SideEffect": "1897 842",
        "SideEffectArg": "2 250 2 100 50",
    },
    {
        "ID": 36799,
        "Name": "永世相约",
        "Category": 2,
        "Type": 100,
        "Power": 9,
        "MaxPP": 9,
        "Accuracy": 99,
        "SideEffect": "1454",
        "SideEffectArg": "7 25 5 20",
    },
    {
        "ID": 28411,
        "Name": "如日方升",
        "Category": 4,
        "Type": 8,
        "Power": 0,
        "MaxPP": 5,
        "Accuracy": 100,
        "MustHit": 1,
        "SideEffect": "191 2060 854 843",
        "SideEffectArg": "4 3 100 7 3 100 2 2",
    },
    {
        "ID": 36800,
        "Name": "圣钧无双陨",
        "Category": 1,
        "Type": 100,
        "Power": 140,
        "MaxPP": 5,
        "Accuracy": 100,
        "MustHit": 1,
        "SideEffect": "426 2061",
        "SideEffectArg": "2 100 2",
    },
    {
        "ID": 28412,
        "Name": "遂心快意",
        "Category": 4,
        "Type": 8,
        "Power": 0,
        "MaxPP": 5,
        "Accuracy": 100,
        "MustHit": 1,
        "SideEffect": "521 1020 597 693",
        "SideEffectArg": "1 4 3 2 100",
    },
    {
        "ID": 36801,
        "Name": "苍灵衔乐",
        "Category": 2,
        "Type": 100,
        "Power": 150,
        "MaxPP": 5,
        "Accuracy": 99,
        "SideEffect": "699 1568 852",
        "SideEffectArg": "30 3",
    },
    {
        "ID": 36802,
        "Name": "清璨星辰落",
        "Category": 1,
        "Type": 100,
        "Power": 160,
        "MaxPP": 5,
        "Accuracy": 100,
        "MustHit": 1,
        "SideEffect": "1843 933 1827 853",
        "SideEffectArg": "300 100 9 1 1 25 10 45",
    }
]
for i in temp:
    Move.append(i)


"""技能代码"""
# url8 = "http://seerh5.61.com/resource/config/xml/"+xml["effectInfo.json"]
# response = urllib.request.urlopen(url8)
# data = json.load(response)
parse_and_dump_effect_info(
    (data_path / "effectInfo.bytes"),
    (data_path / "effectInfo.json"),
)
with open((data_path / "effectInfo.json"), 'r', encoding='utf-8') as f:
    data = json.load(f)
root = data["root"]
ParamType = root["param_type"]
temp = {"id": 114514, "desc": "火火自用标记……"}
ParamType.append(temp)
Effect = root["effect"]
temp = {"id": 21, "args_num": 3, "info": "作用{0}回合，每回合反弹对手1/{2}的伤害"}
Effect.append(temp)
temp = {"id": 31, "args_num": 2, "info": "1回合做{0}~{1}次攻击"}
Effect.append(temp)
temp = {"id": 41, "args_num": 2, "info": "{0}回合本方受到的火系攻击伤害减半"}
Effect.append(temp)
temp = {"id": 42, "args_num": 2, "info": "{0}回合自己使用电招式伤害×2"}
Effect.append(temp)
temp = {"id": 174, "args_num": 5, "info": "{0}回合内，若对手使用属性攻击则{3}%自身{4}", "param": [114514,4,4]}
Effect.append(temp)
# temp = {"id": 174, "args_num": 5, "info": "{0}回合内，若对手使用{2}攻击则{3}%自身{4}"}
# Effect.append(temp)
# temp = {"id": 114514, "args_num": 1, "info": "自身为最后一只存活精灵时，此技能转化为与自身系别相同的特殊攻击技能，且对方每比己方多存活一只精灵，威力提升{0}点"}
# Effect.append(temp)
parse_and_dump_skill_effect(
    (data_path / "skill_effect.bytes"),
    (data_path / "skill_effect.json"),
)


"""属性"""
# url9 = "http://seerh5.61.com/resource/config/xml/"+xml["skillTypes.json"]
# response = urllib.request.urlopen(url9)
# data = json.load(response)
parse_and_dump_skilltypes(
    (data_path / "skillTypes.bytes"),
    (data_path / "skillTypes.json"),
)
with open((data_path / "skillTypes.json"), 'r', encoding='utf-8') as f:
    data = json.load(f)
stitem = data["root"]


"""其他"""
parse_and_dump_pet_skin(
    (data_path / "pet_skin.bytes"),
    (data_path / "pet_skin.json"),
)
parse_and_dump_pvp_vote(
    (data_path / "pvp_vote.bytes"),
    (data_path / "pvp_vote.json"),
)
parse_and_dump_pvp_ban(
    (data_path / "pvp_ban.bytes"),
    (data_path / "pvp_ban.json"),
)
parse_and_dump_pvp_ban_expert(
    (data_path / "pvp_ban_expert.bytes"),
    (data_path / "pvp_ban_expert.json"),
)
parse_and_dump_gem(
    (data_path / "gems.bytes"),
    (data_path / "gems.json"),
)
parse_and_dump_item(
    (data_path / "itemsOptimizeCat.bytes"),
    (data_path / "itemsOptimizeCat.json"),
)
parse_and_dump_sp_hide_moves(
    (data_path / "sp_hide_moves.bytes"),
    (data_path / "sp_hide_moves.json"),
)





class Skill:
    """技能类，模仿原代码中的Skill类"""
    def __init__(self, id, tag, name, cat, acc, pp):
        self.id = id
        self.tag = tag
        self.name = name
        self.cat = cat
        self.acc = acc
        self.pp = pp
        self.type = 0
        self.power = 0
        self.cri = 1
        self.pri = 0
        self.id_list = []
        self.args_list = []
        self.n = 0
        self.txt = []

def generate_skill_effects():
    """生成技能效果文本的主函数"""
    # 加载moves.json数据
    moves_file = data_path / "moves.json"
    moves_file2 = data_path / "moves_unity.json"

    if not moves_file.exists():
        print(f"错误：找不到moves.json文件：{moves_file}")
        return

    with open(moves_file, 'r', encoding='utf-8') as f:
        moves_data = json.load(f)

    with open(moves_file2, 'r', encoding='utf-8') as f:
        moves_data2 = json.load(f)

    moves = moves_data["MovesTbl"]["Moves"]["Move"]
    movesu = moves_data2["root"]["moves"]["move"]
    temp = [
        {
            "id": 36798,
            "name": "生尘眷盼",
            "category": 2,
            "type": 100,
            "power": 90,
            "max_pp": 10,
            "accuracy": 99,
            "priority": 3,
            "side_effect": [1897, 842],
            "side_effect_arg": [2, 250, 2, 100, 50],
        },
        {
            "id": 36799,
            "name": "永世相约",
            "category": 2,
            "type": 100,
            "power": 9,
            "max_pp": 9,
            "accuracy": 99,
            "side_effect": [1454],
            "side_effect_arg": [7, 25, 5, 20],
        },
        {
            "id": 28411,
            "name": "如日方升",
            "category": 4,
            "type": 8,
            "power": 0,
            "max_pp": 5,
            "accuracy": 100,
            "must_hit": 1,
            "side_effect": [191, 2060, 854, 843],
            "side_effect_arg": [4, 3, 100, 7, 3, 100, 2, 2],
        },
        {
            "id": 36800,
            "name": "圣钧无双陨",
            "category": 1,
            "type": 100,
            "power": 140,
            "max_pp": 5,
            "accuracy": 100,
            "must_hit": 1,
            "side_effect": [426, 2061],
            "side_effect_arg": [2, 100, 2],
        },
        {
            "id": 28412,
            "name": "遂心快意",
            "category": 4,
            "type": 8,
            "power": 0,
            "max_pp": 5,
            "accuracy": 100,
            "must_hit": 1,
            "side_effect": [521, 1020, 597, 693],
            "side_effect_arg": [1, 4, 3, 2, 100],
        },
        {
            "id": 36801,
            "name": "苍灵衔乐",
            "category": 2,
            "type": 100,
            "power": 150,
            "max_pp": 5,
            "accuracy": 99,
            "side_effect": [699, 1568, 852],
            "side_effect_arg": [30, 3],
        },
        {
            "id": 36802,
            "name": "清璨星辰落",
            "category": 1,
            "type": 100,
            "power": 160,
            "max_pp": 5,
            "accuracy": 100,
            "must_hit": 1,
            "side_effect": [1843, 933, 1827, 853],
            "side_effect_arg": [300, 100, 9, 1, 1, 25, 10, 45],
        }
    ]
    for i in temp:
        movesu.append(i)

    print(f"开始处理 {len(movesu)} 个技能...")

    # 为每个技能生成效果文本
    for move in movesu:
        try:
            move_id = move["id"]

            # 创建Skill对象，只设置确实存在的属性
            skill = Skill(
                id=move_id,
                tag=[''],  # 默认空标签
                name=move.get("name", f""),
                cat=move.get("category", 0),
                acc=100,  # 临时默认值，后续会覆盖
                pp=10     # 临时默认值，后续会覆盖
            )

            # 只设置确实存在的属性，不设置任何默认值
            if "accuracy" in move:
                skill.acc = move["accuracy"]
            if "max_pp" in move:
                skill.pp = move["max_pp"]

            # 只在非属性攻击技能且属性存在时设置
            if skill.cat != 4:  # 非属性攻击技能
                skill.type = move.get("type", 0)
                skill.power = move.get("power", 0)
                skill.cri = move.get("crit_rate", 1)
                for m in moves:
                    if m["ID"] == move_id:
                        if "CritRate" in m:
                            skill.cri = m["CritRate"]
                        else:
                            skill.cri = 1
                        break
                move["crit_rate"] = skill.cri

            # 只在MustHit存在时设置必中
            if "must_hit" in move and move["must_hit"] == 1:
                skill.acc = "必中"

            # 只在Priority存在时设置优先级
            if "priority" in move:
                skill.pri = move["priority"]
                if isinstance(skill.pri, str):
                    skill.pri = int(skill.pri) if skill.pri else 0

            if len(move["side_effect"]) != 0:
                skill.id_list = [str(n) for n in move["side_effect"]]
            if len(move["side_effect_arg"]) != 0:
                skill.args_list = [str(n) for n in move["side_effect_arg"]]

            # 只在AtkNum存在时设置攻击目标数
            if "atk_num" in move:
                skill.n = move["atk_num"]

            # 生成效果文本
            generate_skill_text(skill)

            # 将效果文本添加到move数据中
            move["EffectText"] = skill.txt

            # 打印进度
            if move_id % 1000 == 0:
                print(f"已处理 {move_id} 个技能...")

        except Exception as e:
            print(f"处理技能 {move.get('id', '未知')} 时出错: {e}")
            continue

    print("技能效果生成完成，正在保存...")

    # 保存更新后的moves.json
    with open(data_path / "moves_done.json", 'w', encoding='utf-8') as f:
        json.dump(moves_data2, f, ensure_ascii=False)

    print(f"保存完成！共处理了 {len(moves)} 个技能。")

def generate_skill_text(skill):
    """生成技能效果文本（模仿原代码逻辑）"""
    tl = []

    # 处理优先级
    if skill.pri > 0:
        if skill.acc == '必中':
            tl.append(f'先制+{skill.pri}; 必中;')
        else:
            tl.append(f'先制+{skill.pri};')
    elif skill.pri < 0:
        if skill.acc == '必中':
            tl.append(f'先制{skill.pri}; 必中;')
        else:
            tl.append(f'先制{skill.pri};')
    else:
        if skill.acc == '必中':
            tl.append("必中;")

    # 处理攻击目标数量
    if len(skill.id_list) == 0:
        if skill.n == 0:
            if skill.cat == 1:
                tl.append('造成物理攻击伤害')
            else:
                tl.append('造成特殊攻击伤害')
        else:
            tl.append(f'组队时可以影响{skill.n}个目标')
    else:
        # 处理技能效果
        for effect_id in skill.id_list:
            for effect in Effect:
                if effect_id == str(effect["id"]):
                    try:
                        effect_text = generate_effect_text(skill, effect)
                        if effect_text:
                            # 把这里的effect_text[0]里的[color=#xxxxxx]等标签去掉
                            effect_text[0] = re.sub(r'\[color=#[0-9a-fA-F]+\]', '', effect_text[0])
                            effect_text[0] = re.sub(r'\[/color\]', '', effect_text[0])
                            effect_text[0] = re.sub(r'\[b\]', '', effect_text[0])
                            effect_text[0] = re.sub(r'\[/b\]', '', effect_text[0])
                            effect_text[0] = re.sub(r'\[indent=\d+\]', '', effect_text[0])
                            effect_text[0] = re.sub(r'\[sprite=\d+\]', '', effect_text[0])
                            effect_text[0] = re.sub(r'\[/sprite\]', '', effect_text[0])
                            tl.append(effect_text)
                    except Exception as e:
                        print(f"处理技能ID {skill.id} 效果ID {effect_id} 时出错: {e}")
                    break

    skill.txt = tl

def generate_effect_text(skill, effect):
    """生成单个效果的文本"""
    effect_id = effect["id"]
    args_num = effect.get("args_num", 0)
    info = effect.get("info", "")
    param = effect.get("param", [])

    # 特殊处理某些效果ID
    if effect_id in [21, 41, 42]:
        temp = []
        for i in range(args_num):
            temp.append(skill.args_list.pop(0))
        if temp[0] == temp[1]:
            return [info.format(*temp).rstrip(), f'{effect_id}']
        else:
            temp[0] = temp[0] + '~' + temp[1]
            return [info.format(*temp).rstrip(), f'{effect_id}']

    elif effect_id in [451]:
        temp = [skill.args_list[0], '1']
        return [info.format(*temp).rstrip(), f'{effect_id}']

    elif args_num == 0:
        return [info.rstrip(), f'{effect_id}']

    else:
        try:
            param_list = [param[i:i+3] for i in range(0, len(param), 3)]
            temp = []
            for i in range(args_num):
                temp.append(skill.args_list.pop(0))

            for p in param_list:
                for pt in ParamType:
                    if p[0] == pt["id"]:
                        if p[0] == 0 and args_num >= 6:
                            changes = []
                            for i in range(6):
                                if int(temp[p[1] + i]) > 0:
                                    changes.append(list(pt["params"].split('|'))[i] + '+' + temp[p[1]+i])
                                elif int(temp[p[1] + i]) < 0:
                                    changes.append(list(pt["params"].split('|'))[i] + temp[p[1]+i])
                            temp[p[1]] = '，'.join(changes)

                        elif p[0] == 14:
                            if int(temp[p[1]]) > 0:
                                temp[p[1]] = '+' + temp[p[1]]

                        elif p[0] == 16 and args_num >= 6:
                            changes = []
                            for i in range(6):
                                if int(temp[p[1] + i]) > 0:
                                    changes.append(list(pt["params"].split('|'))[i])
                            temp[p[1]] = '、'.join(changes)

                        elif p[0] == 20:
                            if int(temp[p[1]]) >= 40:
                                temp[p[1]] = '全部'

                        elif p[0] == 22:
                            for item in stitem:
                                if item["id"] == int(temp[p[1]]):
                                    temp[p[1]] = item["cn"]
                                    break

                        elif p[0] == 24 and args_num >= 6:
                            changes = []
                            for i in range(6):
                                if int(temp[p[1] + i]) > 0:
                                    changes.append(list(pt["params"].split('|'))[i] + '-' + temp[p[1]+i])
                            temp[p[1]] = '、'.join(changes)

                        elif p[0] == 114514:
                            if int(temp[p[1] - 2]) == -1:
                                temp[p[1]] = "速度等级+1"
                            else:
                                temp[p[1]] = "特攻和速度等级+1"
                        
                        else:
                            # 普通参数类型处理
                            params = pt.get("params", "").split('|')
                            if params and int(temp[p[1]]) < len(params):
                                temp[p[1]] = params[int(temp[p[1]])]

                        break

            return [info.format(*temp).rstrip(), f'{effect_id}']

        except Exception as e:
            try:
                temp = []
                for i in range(args_num):
                    temp.append(skill.args_list.pop(0))
                return [info.format(*temp).rstrip(), f'{effect_id}']
            except Exception as e2:
                return [info.rstrip(), f'{effect_id}']

print("开始提取技能效果文本...")
generate_skill_effects()
print("技能效果提取完成！")



TAG_PATTERN = re.compile(r"(<indent=\d+>|<sprite=\d+>|<color=[^>]+>|</color>|<b>|</b>)")
RE_INDENT = re.compile(r"<indent=(\d+)>")
RE_SPRITE = re.compile(r"<sprite=(\d+)>")
RE_COLOR_OPEN = re.compile(r"<color=([^>]+)>")

def parse_line_segments_and_sprites(line: str):
    segments = []
    sprites = []
    color_stack = []
    bold = False

    def emit_text(text: str):
        if text == "":
            return
        segments.append({
            "text": text,
            "color": color_stack[-1] if color_stack else None,
            "bold": bold or None,
        })

    parts = TAG_PATTERN.split(line)
    for part in parts:
        if not part:
            continue
        if part.startswith("<indent="):
            # handled at line level; ignore here
            continue
        if part.startswith("<sprite="):
            try:
                sprite_id = int(RE_SPRITE.match(part).group(1))
            except Exception:
                sprite_id = None
            sprites.append(sprite_id)
            continue
        if part.startswith("<color="):
            m = RE_COLOR_OPEN.match(part)
            color_stack.append(m.group(1) if m else None)
            continue
        if part == "</color>":
            if color_stack:
                color_stack.pop()
            continue
        if part == "<b>":
            bold = True
            continue
        if part == "</b>":
            bold = False
            continue
        # plain text
        emit_text(part)

    # 规范化：去掉 None 字段
    for seg in segments:
        if "bold" in seg and seg["bold"] is None:
            del seg["bold"]
        if "color" in seg and seg["color"] is None:
            del seg["color"]
    return segments, sprites

def parse_rich_text_to_tree(text: str, indent_unit: int = 16) -> list:
    lines = text.replace("\r\n", "\n").split("\n")
    root = {"level": -1, "children": []}
    stack = [root]

    for raw in lines:
        # level
        indents = RE_INDENT.findall(raw)
        level = int(indents[-1]) // indent_unit if indents else 0
        segments, sprites = parse_line_segments_and_sprites(raw)
        # 跳过空白行（既无文字段也无精灵）
        if not segments and not sprites:
            continue
        node = {"level": level, "sprites": sprites or [], "segments": segments, "children": []}

        # attach to proper parent
        while stack and stack[-1]["level"] >= level:
            stack.pop()
        stack[-1]["children"].append(node)
        stack.append(node)

    return root["children"]

def export_rich_text(text: str, output_json_path: str = None, indent_unit: int = 16):
    """从富文本字符串解析为层级 JSON。

    参数:
      - text: 含 <indent=>/<sprite=>/<color=>/<b> 标签的原始富文本
      - output_json_path: 可选，若提供则将结果写入该路径
      - indent_unit: 一个缩进层级对应的数值（默认 16）

    返回: 树状结构(list)
    """
    tree = parse_rich_text_to_tree(text, indent_unit=indent_unit)
    if output_json_path:
        with open(output_json_path, "w", encoding="utf-8") as f:
            json.dump(tree, f, ensure_ascii=False)
    return tree

input_json_path = str(data_path / "petEffectIcon.json")
output_json_path = str(data_path / "rich_text_tree.json")

with open(input_json_path, "r", encoding="utf-8") as f:
    data = json.load(f)

new_data = []
for item in data["data"]:
    if item["petid"] != 4447:
        tree = export_rich_text(item["Desc"])
    else:
        tree = [
            {
                "level": 1,
                "sprites": [
                    0
                ],
                "segments": [
                    {
                        "text": "触发效果："
                    }
                ],
                "children": [
                    {
                        "level": 2,
                        "sprites": [
                            3
                        ],
                        "segments": [
                            {
                                "text": "自身技能命中时"
                            },
                            {
                                "text": "消除",
                                "color": "#64F9FA"
                            },
                            {
                                "text": "对手所有"
                            },
                            {
                                "text": "护盾",
                                "bold": True
                            },
                            {
                                "text": "效果"
                            }
                        ],
                        "children": [
                            {
                                "level": 3,
                                "sprites": [
                                    4
                                ],
                                "segments": [
                                    {
                                        "text": "消除成功则"
                                    },
                                    {
                                        "text": "获得",
                                        "color": "#64F9FA"
                                    },
                                    {
                                        "text": "等量的"
                                    },
                                    {
                                        "text": "护盾",
                                        "bold": True
                                    },
                                    {
                                        "text": "值并"
                                    },
                                    {
                                        "text": "附加",
                                        "color": "#64F9FA"
                                    },
                                    {
                                        "text": "等量的"
                                    },
                                    {
                                        "text": "固定伤害",
                                        "bold": True
                                    }
                                ]
                            }
                        ]
                    }
                ]
            },
            {
                "level": 1,
                "sprites": [
                    0
                ],
                "segments": [
                    {
                        "text": "战阶结束效果："
                    }
                ],
                "children": [
                    {
                        "level": 2,
                        "sprites": [
                            3
                        ],
                        "segments": [
                            {
                                "text": "战斗阶段结束时"
                            },
                            {
                                "text": "恢复",
                                "color": "#64F9FA"
                            },
                            {
                                "text": "等同于自身当前护盾值的"
                            },
                            {
                                "text": "体力",
                                "bold": True
                            }
                        ],
                        "children": [
                        ]
                    }
                ]
            }
        ]
    temp = {}
    temp["id"] = item["petid"]
    temp["pve"] = item["affectedBoss"]
    temp["text"] = tree
    new_data.append(temp)

os.makedirs(os.path.dirname(output_json_path), exist_ok=True)
with open(output_json_path, "w", encoding="utf-8") as f:
    json.dump(new_data, f, ensure_ascii=False)



import simplejson as json

def db_effectag():
    with open(data_path / "effectag.json", 'r', encoding='utf-8') as f:
        data = json.load(f)
    root = data["data"]


    def create_scheme_database(db_path):
        # 确保数据库所在目录存在
        db_dir = os.path.dirname(db_path)
        if db_dir and not os.path.exists(db_dir):
            os.makedirs(db_dir)

        try:
            # 连接数据库（文件不存在则自动创建）
            conn = sqlite3.connect(db_path)
            cursor = conn.cursor()

            create_table_sql = """
            CREATE TABLE IF NOT EXISTS effectag (
                dbid INTEGER PRIMARY KEY AUTOINCREMENT,
                id INTEGER NOT NULL,        -- 
                tag TEXT NOT NULL  -- 
            );
            """
            cursor.execute(create_table_sql)
            conn.commit()

            print(f"✅ 数据库创建成功：{db_path}")
            print("✅ 数据表创建成功（字段结构完全匹配要求）")

        except sqlite3.Error as e:
            print(f"❌ 数据库创建失败：{e}")
        finally:
            # 确保连接关闭
            if conn:
                conn.close()

    DB_PATH = str(data_path / "effectag.db")  # CI 输出路径

    try:
        os.remove(DB_PATH)
    except:
        pass

    create_scheme_database(DB_PATH)

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row  # 设置为字典格式
    cursor = conn.cursor()

    for i in root:
        try:
            id = i.get("id", 0)
            tag = i.get("tag", '')
            cursor.execute("""
                INSERT INTO effectag (id, tag)
                VALUES (?, ?)
                """,(id, tag)
            )
        except Exception as e:
            print(f"❌ 插入数据失败：{e}，数据内容：{i}")
    conn.commit()
db_effectag()
def db_effectbuff():
    with open(data_path / "effectbuff.json", 'r', encoding='utf-8') as f:
        data = json.load(f)
    root = data["root"]["Buff"]


    def create_scheme_database(db_path):
        # 确保数据库所在目录存在
        db_dir = os.path.dirname(db_path)
        if db_dir and not os.path.exists(db_dir):
            os.makedirs(db_dir)

        try:
            # 连接数据库（文件不存在则自动创建）
            conn = sqlite3.connect(db_path)
            cursor = conn.cursor()

            create_table_sql = """
            CREATE TABLE IF NOT EXISTS effectbuff (
                dbid INTEGER PRIMARY KEY AUTOINCREMENT,
                Desc TEXT NOT NULL,        -- 
                ID INTEGER NOT NULL,        -- 
                Kind INTEGER NOT NULL,        -- 
                Name TEXT NOT NULL  -- 
            );
            """
            cursor.execute(create_table_sql)
            conn.commit()

            print(f"✅ 数据库创建成功：{db_path}")
            print("✅ 数据表创建成功（字段结构完全匹配要求）")

        except sqlite3.Error as e:
            print(f"❌ 数据库创建失败：{e}")
        finally:
            # 确保连接关闭
            if conn:
                conn.close()

    DB_PATH = str(data_path / "effectbuff.db")  # CI 输出路径

    try:
        os.remove(DB_PATH)
    except:
        pass

    create_scheme_database(DB_PATH)

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row  # 设置为字典格式
    cursor = conn.cursor()

    for i in root:
        try:
            Desc = i.get("Desc", '')
            ID = i.get("ID", 0)
            Kind = i.get("Kind", 0)
            Name = i.get("Name", '')
            cursor.execute("""
                INSERT INTO effectbuff (Desc, ID, Kind, Name)
                VALUES (?, ?, ?, ?)
                """,(Desc, ID, Kind, Name)
            )
        except Exception as e:
            print(f"❌ 插入数据失败：{e}，数据内容：{i}")
    conn.commit()
db_effectbuff()
def db_effectDes():
    with open(data_path / "effectDes.json", 'r', encoding='utf-8') as f:
        data = json.load(f)
    root = data["root"]["item"]


    def create_scheme_database(db_path):
        # 确保数据库所在目录存在
        db_dir = os.path.dirname(db_path)
        if db_dir and not os.path.exists(db_dir):
            os.makedirs(db_dir)

        try:
            # 连接数据库（文件不存在则自动创建）
            conn = sqlite3.connect(db_path)
            cursor = conn.cursor()

            create_table_sql = """
            CREATE TABLE IF NOT EXISTS effectDes (
                dbid INTEGER PRIMARY KEY AUTOINCREMENT,
                desc TEXT NOT NULL,        -- 
                icon INTEGER NOT NULL,        -- 
                id INTEGER NOT NULL,        -- 
                kind INTEGER NOT NULL,        -- 
                kinddes TEXT NOT NULL,        -- 
                link TEXT NOT NULL,        -- 
                monster TEXT NOT NULL,        -- 
                tab INTEGER NOT NULL  -- 
            );
            """
            cursor.execute(create_table_sql)
            conn.commit()

            print(f"✅ 数据库创建成功：{db_path}")
            print("✅ 数据表创建成功（字段结构完全匹配要求）")

        except sqlite3.Error as e:
            print(f"❌ 数据库创建失败：{e}")
        finally:
            # 确保连接关闭
            if conn:
                conn.close()

    DB_PATH = str(data_path / "effectDes.db")  # CI 输出路径

    try:
        os.remove(DB_PATH)
    except:
        pass

    create_scheme_database(DB_PATH)

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row  # 设置为字典格式
    cursor = conn.cursor()

    for i in root:
        try:
            desc = i.get("desc", '')
            icon = i.get("icon", 0)
            id = i.get("id", 0)
            kind = i.get("kind", 0)
            kinddes = i.get("kinddes", '')
            link = i.get("link", '')
            monster = i.get("monster", '')
            tab = i.get("tab", 0)
            cursor.execute("""
                INSERT INTO effectDes (desc, icon, id, kind, kinddes, link, monster, tab)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,(desc, icon, id, kind, kinddes, link, monster, tab)
            )
        except Exception as e:
            print(f"❌ 插入数据失败：{e}，数据内容：{i}")
    conn.commit()
db_effectDes()
def db_effectIcon():
    with open(data_path / "effectIcon.json", 'r', encoding='utf-8') as f:
        data = json.load(f)
    root = data["root"]["effect"]


    def create_scheme_database(db_path):
        # 确保数据库所在目录存在
        db_dir = os.path.dirname(db_path)
        if db_dir and not os.path.exists(db_dir):
            os.makedirs(db_dir)

        try:
            # 连接数据库（文件不存在则自动创建）
            conn = sqlite3.connect(db_path)
            cursor = conn.cursor()

            create_table_sql = """
            CREATE TABLE IF NOT EXISTS effectIcon (
                dbid INTEGER PRIMARY KEY AUTOINCREMENT,
                analyze TEXT NOT NULL,        -- 
                args TEXT NOT NULL,        -- 
                come TEXT NOT NULL,        -- 
                des TEXT NOT NULL,        -- 
                tag TEXT NOT NULL,        -- 
                tips TEXT NOT NULL,        -- 
                kind TEXT NOT NULL,        -- 
                pet_id TEXT NOT NULL,        -- 
                specific_id TEXT NOT NULL,        -- 
                effect_id INTEGER NOT NULL,        -- 
                icon_id INTEGER NOT NULL,        -- 
                id INTEGER NOT NULL,        -- 
                intensify INTEGER NOT NULL,        -- 
                is_adv INTEGER NOT NULL,        -- 
                label INTEGER NOT NULL,        -- 
                limited_type INTEGER NOT NULL,        -- 
                target INTEGER NOT NULL,        -- 
                to_ INTEGER NOT NULL,        -- 
                type INTEGER NOT NULL  -- 
            );
            """
            cursor.execute(create_table_sql)
            conn.commit()

            print(f"✅ 数据库创建成功：{db_path}")
            print("✅ 数据表创建成功（字段结构完全匹配要求）")

        except sqlite3.Error as e:
            print(f"❌ 数据库创建失败：{e}")
        finally:
            # 确保连接关闭
            if conn:
                conn.close()

    DB_PATH = str(data_path / "effectIcon.db")  # CI 输出路径

    try:
        os.remove(DB_PATH)
    except:
        pass

    create_scheme_database(DB_PATH)

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row  # 设置为字典格式
    cursor = conn.cursor()

    for i in root:
        try:
            analyze = i.get("analyze", '')
            args = i.get("args", '')
            come = i.get("come", '')
            des = str(i.get("des", []))
            tag = str(i.get("tag", []))
            tips = i.get("tips", '')
            kind = str(i.get("kind", []))
            pet_id = str(i.get("pet_id", []))
            specific_id = str(i.get("specific_id", []))
            effect_id = i.get("effect_id", 0)
            icon_id = i.get("icon_id", 0)
            id = i.get("id", 0)
            intensify = i.get("intensify", 0)
            is_adv = i.get("is_adv", 0)
            label = i.get("label", 0)
            limited_type = i.get("limited_type", 0)
            target = i.get("target", 0)
            to_ = i.get("to", 0)
            type = i.get("type", 0)
            cursor.execute("""
                INSERT INTO effectIcon (analyze, args, come, des, tag, tips, kind, pet_id, specific_id, effect_id, icon_id, id, intensify, is_adv, label, limited_type, target, to_, type)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,(analyze, args, come, des, tag, tips, kind, pet_id, specific_id, effect_id, icon_id, id, intensify, is_adv, label, limited_type, target, to_, type)
            )
        except Exception as e:
            print(f"❌ 插入数据失败：{e}，数据内容：{i}")
    conn.commit()
db_effectIcon()
def db_effectInfo():
    with open(data_path / "effectInfo.json", 'r', encoding='utf-8') as f:
        data = json.load(f)
    root1 = data["root"]["effect"]
    root2 = data["root"]["param_type"]


    def create_scheme_database(db_path):
        # 确保数据库所在目录存在
        db_dir = os.path.dirname(db_path)
        if db_dir and not os.path.exists(db_dir):
            os.makedirs(db_dir)

        try:
            # 连接数据库（文件不存在则自动创建）
            conn = sqlite3.connect(db_path)
            cursor = conn.cursor()

            create_table_sql = """
            CREATE TABLE IF NOT EXISTS effect (
                dbid INTEGER PRIMARY KEY AUTOINCREMENT,
                analyze TEXT NOT NULL,        -- 
                info TEXT NOT NULL,        -- 
                param TEXT NOT NULL,        -- 
                args_num INTEGER NOT NULL,        -- 
                id INTEGER NOT NULL,        -- 
                key TEXT NOT NULL,        -- 
                type INTEGER NOT NULL        -- 
            );
            """
            cursor.execute(create_table_sql)
            create_table_sql = """
            CREATE TABLE IF NOT EXISTS param_type (
                dbid INTEGER PRIMARY KEY AUTOINCREMENT,
                id INTEGER NOT NULL,        -- 
                params TEXT NOT NULL        -- 
            );
            """
            cursor.execute(create_table_sql)
            conn.commit()

            print(f"✅ 数据库创建成功：{db_path}")
            print("✅ 数据表创建成功（字段结构完全匹配要求）")

        except sqlite3.Error as e:
            print(f"❌ 数据库创建失败：{e}")
        finally:
            # 确保连接关闭
            if conn:
                conn.close()

    DB_PATH = str(data_path / "effectInfo.db")  # CI 输出路径

    try:
        os.remove(DB_PATH)
    except:
        pass

    create_scheme_database(DB_PATH)

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row  # 设置为字典格式
    cursor = conn.cursor()

    for i in root1:
        try:
            analyze = i.get("analyze", "")
            info = i.get("info", "")
            param = str(i.get("param", []))
            args_num = i.get("args_num", 0)
            id = i.get("id", 0)
            key = i.get("key", "")
            type = i.get("type", 0)
            cursor.execute("""
                INSERT INTO effect (analyze, info, param, args_num, id, key, type)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """, (analyze, info, param, args_num, id, key, type)
            )
        except Exception as e:
            print(f"❌ 插入数据失败：{e}，数据内容：{i}")
    for i in root2:
        try:
            id = i.get("id", 0)
            params = i.get("params", "")
            cursor.execute("""
                INSERT INTO param_type (id, params)
                VALUES (?, ?)
                """, (id, params)
            )
        except Exception as e:
            print(f"❌ 插入数据失败：{e}，数据内容：{i}")
    conn.commit()
db_effectInfo()
def db_gems():
    with open(data_path / "gems.json", 'r', encoding='utf-8') as f:
        data = json.load(f)
    gem = data["gems"]["gem"]


    def create_scheme_database(db_path):
        # 确保数据库所在目录存在
        db_dir = os.path.dirname(db_path)
        if db_dir and not os.path.exists(db_dir):
            os.makedirs(db_dir)

        try:
            # 连接数据库（文件不存在则自动创建）
            conn = sqlite3.connect(db_path)
            cursor = conn.cursor()

            create_table_sql = """
            CREATE TABLE IF NOT EXISTS gems (
                dbid INTEGER PRIMARY KEY AUTOINCREMENT,
                category INTEGER NOT NULL,        -- 
                decompose_prob INTEGER NOT NULL,  -- 
                des TEXT NOT NULL,  -- 
                equit_lv1_cnt1 INTEGER NOT NULL,  -- 
                gid INTEGER NOT NULL,  -- 
                lv INTEGER NOT NULL,  -- 
                name TEXT NOT NULL,  -- 
                effect_id INTEGER NOT NULL,  -- 
                param INTEGER NOT NULL,  -- 
                upgrade_gem_id INTEGER NOT NULL  -- 
            );
            """
            cursor.execute(create_table_sql)
            conn.commit()

            print(f"✅ 数据库创建成功：{db_path}")
            print("✅ 数据表创建成功（字段结构完全匹配要求）")

        except sqlite3.Error as e:
            print(f"❌ 数据库创建失败：{e}")
        finally:
            # 确保连接关闭
            if conn:
                conn.close()

    DB_PATH = str(data_path / "gems.db")  # CI 输出路径

    try:
        os.remove(DB_PATH)
    except:
        pass

    create_scheme_database(DB_PATH)

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row  # 设置为字典格式
    cursor = conn.cursor()

    Des = ''
    Ugi = 0
    for i in gem:
        try:
            category = i.get("category", 0)
            decompose_prob = i.get("decompose_prob", 0)
            des = i.get("des", '')
            equit_lv1_cnt1 = i.get("equit_lv1_cnt1", 0)
            gid = i.get("gid", 0)
            if Ugi == gid:
                des = Des
            Des = des
            lv = i.get("lv", 0)
            name = i.get("name", '')
            effect_id = i["skill_effects"][0]["effect"]["effect_id"]
            try:
                param = i["skill_effects"][0]["effect"]["param"][0]
            except:
                param = 0
            upgrade_gem_id = i.get("upgrade_gem_id", 0)
            Ugi = upgrade_gem_id
            cursor.execute("""
                INSERT INTO gems (category, decompose_prob, des, equit_lv1_cnt1, gid, lv, name, effect_id, param, upgrade_gem_id)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,(category, decompose_prob, des, equit_lv1_cnt1, gid, lv, name, effect_id, param, upgrade_gem_id)
            )
        except Exception as e:
            print(f"❌ 插入数据失败：{e}，数据内容：{i}")
    conn.commit()
db_gems()
def db_items():
    with open(data_path / "itemsOptimizeCat.json", 'r', encoding='utf-8') as f:
        data = json.load(f)
    root = data["root"]
    with open(data_path / "itemsTip.json", 'r', encoding='utf-8') as f:
        data = json.load(f)
    roots = data["root"]


    def create_scheme_database(db_path):
        # 确保数据库所在目录存在
        db_dir = os.path.dirname(db_path)
        if db_dir and not os.path.exists(db_dir):
            os.makedirs(db_dir)

        try:
            # 连接数据库（文件不存在则自动创建）
            conn = sqlite3.connect(db_path)
            cursor = conn.cursor()

            create_table_sql = """
            CREATE TABLE IF NOT EXISTS items (
                dbid INTEGER PRIMARY KEY AUTOINCREMENT,
                ID INTEGER NOT NULL,        -- 
                Name TEXT NOT NULL,  -- 
                Max INTEGER NOT NULL,  -- 
                catName TEXT NOT NULL,  -- 
                LimitPetClass TEXT NOT NULL,  -- 
                des TEXT NOT NULL  -- 
            );
            """
            cursor.execute(create_table_sql)
            conn.commit()

            print(f"✅ 数据库创建成功：{db_path}")
            print("✅ 数据表创建成功（字段结构完全匹配要求）")

        except sqlite3.Error as e:
            print(f"❌ 数据库创建失败：{e}")
        finally:
            # 确保连接关闭
            if conn:
                conn.close()

    DB_PATH = str(data_path / "items.db")  # CI 输出路径

    try:
        os.remove(DB_PATH)
    except:
        pass

    create_scheme_database(DB_PATH)

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row  # 设置为字典格式
    cursor = conn.cursor()

    for i in reversed(root):
        with open(data_path / f"itemsOptimizeCatItems{i['ID']}.json", 'r', encoding='utf-8') as f:
            data = json.load(f)
        r = data["root"]
        for j in r:
            try:
                ID = j.get("ID", 0)
                Name = j.get("Name", '')
                Max = j.get("Max", 0)
                if Max == 0:
                    Max = i["Max"]
                if Max == -294967296:
                    Max = 4000000000
                catName = i.get("Name", '')
                LimitPetClass = j.get("LimitPetClass", '')
                des = ''
                for k in roots:
                    if k["id"] == ID:
                        des = k.get("des", '')
                        break
                cursor.execute("""
                    INSERT INTO items (ID, Name, Max, catName, LimitPetClass, des)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,(ID, Name, Max, catName, LimitPetClass, des)
                )
            except Exception as e:
                print(f"❌ 插入数据失败：{e}，数据内容：{i}")
    conn.commit()
db_items()
def db_mintmark():
    with open(data_path / "mintmark.json", 'r', encoding='utf-8') as f:
        data = json.load(f)
    root1 = data["MintMarks"]["MintMark"]
    root2 = data["MintMarks"]["MintMarkClass"]


    def create_scheme_database(db_path):
        # 确保数据库所在目录存在
        db_dir = os.path.dirname(db_path)
        if db_dir and not os.path.exists(db_dir):
            os.makedirs(db_dir)

        try:
            # 连接数据库（文件不存在则自动创建）
            conn = sqlite3.connect(db_path)
            cursor = conn.cursor()

            create_table_sql = """
            CREATE TABLE IF NOT EXISTS MintMark (
                dbid INTEGER PRIMARY KEY AUTOINCREMENT,
                AttriValue TEXT NOT NULL,        -- 
                BaseAttriValue TEXT NOT NULL,        -- 
                Connect INTEGER NOT NULL,        -- 
                Des TEXT NOT NULL,        -- 
                EffectDes TEXT NOT NULL,        -- 
                ExtraAttriValue TEXT NOT NULL,        -- 
                Grade INTEGER NOT NULL,        -- 
                Hide INTEGER NOT NULL,        -- 
                ID INTEGER NOT NULL,        -- 
                Level INTEGER NOT NULL,        -- 
                Max INTEGER NOT NULL,        -- 
                MaxAttriValue TEXT NOT NULL,        -- 
                MintmarkClass INTEGER NOT NULL,        -- 
                MonsterID TEXT NOT NULL,        -- 
                Quality INTEGER NOT NULL,        -- 
                Rare INTEGER NOT NULL,        -- 
                Rarity INTEGER NOT NULL,        -- 
                TotalConsume INTEGER NOT NULL,        -- 
                Type INTEGER NOT NULL        -- 
            );
            """
            cursor.execute(create_table_sql)
            create_table_sql = """
            CREATE TABLE IF NOT EXISTS MintMarkClass (
                dbid INTEGER PRIMARY KEY AUTOINCREMENT,
                ClassName TEXT NOT NULL,        -- 
                ID INTEGER NOT NULL        -- 
            );
            """
            cursor.execute(create_table_sql)
            conn.commit()

            print(f"✅ 数据库创建成功：{db_path}")
            print("✅ 数据表创建成功（字段结构完全匹配要求）")

        except sqlite3.Error as e:
            print(f"❌ 数据库创建失败：{e}")
        finally:
            # 确保连接关闭
            if conn:
                conn.close()

    DB_PATH = str(data_path / "mintmark.db")  # CI 输出路径

    try:
        os.remove(DB_PATH)
    except:
        pass

    create_scheme_database(DB_PATH)

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row  # 设置为字典格式
    cursor = conn.cursor()

    for i in root1:
        try:
            AttriValue = [0, 0, 0, 0, 0, 0]
            BaseAttriValue = i.get("BaseAttriValue", [])
            Connect = i.get("Connect", 0)
            Des = i.get("Des", "")
            EffectDes = i.get("EffectDes", "")
            ExtraAttriValue = i.get("ExtraAttriValue", [])
            Grade = i.get("Grade", 0)
            Hide = i.get("Hide", 0)
            ID = i.get("ID", 0)
            Level = i.get("Level", 0)
            Max = i.get("Max", 0)
            MaxAttriValue = i.get("MaxAttriValue", [])
            MintmarkClass = i.get("MintmarkClass", 0)
            MonsterID = str(i.get("MonsterID", []))
            Quality = i.get("Quality", 0)
            Rare = i.get("Rare", 0)
            Rarity = i.get("Rarity", 0)
            TotalConsume = i.get("TotalConsume", 0)
            Type = i.get("Type", 0)
            if Grade == 0:
                matches = re.findall(r"(\D+)\+(\d+)", EffectDes)
                attr_list = [[key.strip(), int(value)] for key, value in matches]
                for j in attr_list:
                    match(j[0]):
                        case "攻击":
                            AttriValue[0] += j[1]
                        case "防御":
                            AttriValue[1] += j[1]
                        case "特攻":
                            AttriValue[2] += j[1]
                        case "特防":
                            AttriValue[3] += j[1]
                        case "速度":
                            AttriValue[4] += j[1]
                        case "体力":
                            AttriValue[5] += j[1]
                        case "全属性":
                            for k in range(6):
                                AttriValue[k] += j[1]
            else:
                AttriValue = MaxAttriValue
                for j in range(len(ExtraAttriValue)):
                    AttriValue[j] += ExtraAttriValue[j]
            AttriValue = str(AttriValue)
            BaseAttriValue = str(BaseAttriValue)
            ExtraAttriValue = str(ExtraAttriValue)
            MaxAttriValue = str(MaxAttriValue)
            cursor.execute("""
                INSERT INTO MintMark (AttriValue, BaseAttriValue, Connect, Des, EffectDes, ExtraAttriValue, Grade, Hide, ID, Level, Max, MaxAttriValue, MintmarkClass, MonsterID, Quality, Rare, Rarity, TotalConsume, Type)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (AttriValue, BaseAttriValue, Connect, Des, EffectDes, ExtraAttriValue, Grade, Hide, ID, Level, Max, MaxAttriValue, MintmarkClass, MonsterID, Quality, Rare, Rarity, TotalConsume, Type)
            )
        except Exception as e:
            print(f"❌ 插入数据失败：{e}，数据内容：{i}")
    for i in root2:
        try:
            ClassName = i.get("ClassName", "")
            ID = i.get("ID", 0)
            cursor.execute("""
                INSERT INTO MintMarkClass (ClassName, ID)
                VALUES (?, ?)
                """, (ClassName, ID)
            )
        except Exception as e:
            print(f"❌ 插入数据失败：{e}，数据内容：{i}")
    conn.commit()
db_mintmark()
def db_moves():
    with open(data_path / "moves_done.json", 'r', encoding='utf-8') as f:
        data = json.load(f)
    move = data["root"]["moves"]["move"]


    def create_scheme_database(db_path):
        # 确保数据库所在目录存在
        db_dir = os.path.dirname(db_path)
        if db_dir and not os.path.exists(db_dir):
            os.makedirs(db_dir)

        try:
            # 连接数据库（文件不存在则自动创建）
            conn = sqlite3.connect(db_path)
            cursor = conn.cursor()

            create_table_sql = """
            CREATE TABLE IF NOT EXISTS moves (
                dbid INTEGER PRIMARY KEY AUTOINCREMENT,
                accuracy INTEGER NOT NULL,        -- 
                atk_num INTEGER NOT NULL,  -- 
                atk_type INTEGER NOT NULL,  -- 
                category INTEGER NOT NULL,  -- 
                crit_rate INTEGER NOT NULL,  -- 
                friend_side_effect TEXT NOT NULL,  -- 
                friend_side_effect_arg TEXT NOT NULL,  -- 
                id INTEGER NOT NULL,  -- 
                max_pp INTEGER NOT NULL,  -- 
                mon_id INTEGER NOT NULL,  -- 
                must_hit INTEGER NOT NULL,  -- 
                name TEXT NOT NULL,  -- 
                power INTEGER NOT NULL,        -- 
                priority INTEGER NOT NULL,        -- 
                side_effect TEXT NOT NULL,        -- 
                side_effect_arg TEXT NOT NULL,        -- 
                type INTEGER NOT NULL,        -- 
                info TEXT NOT NULL,        -- 
                ordinary INTEGER NOT NULL,        -- 
                EffectText TEXT NOT NULL        -- 
            );
            """
            cursor.execute(create_table_sql)
            conn.commit()

            print(f"✅ 数据库创建成功：{db_path}")
            print("✅ 数据表创建成功（字段结构完全匹配要求）")

        except sqlite3.Error as e:
            print(f"❌ 数据库创建失败：{e}")
        finally:
            # 确保连接关闭
            if conn:
                conn.close()

    DB_PATH = str(data_path / "moves.db")  # CI 输出路径

    try:
        os.remove(DB_PATH)
    except:
        pass

    create_scheme_database(DB_PATH)

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row  # 设置为字典格式
    cursor = conn.cursor()

    for i in move:
        try:
            accuracy = i.get("accuracy", 100)
            atk_num = i.get("atk_num", 0)
            atk_type = i.get("atk_type", 0)
            category = i.get("category", 0)
            crit_rate = i.get("crit_rate", 1)
            friend_side_effect = str(i.get("friend_side_effect", []))
            friend_side_effect_arg = str(i.get("friend_side_effect_arg", []))
            id = i.get("id", 0)
            max_pp = i.get("max_pp", 0)
            mon_id = i.get("mon_id", 0)
            must_hit = i.get("must_hit", 0)
            name = i.get("name", '')
            power = i.get("power", 0)
            priority = i.get("priority", 0)
            side_effect = str(i.get("side_effect", []))
            side_effect_arg = str(i.get("side_effect_arg", []))
            type = i.get("type", 0)
            info = i.get("info", '')
            ordinary = i.get("ordinary", 0)
            EffectText = str(i.get("EffectText", []))
            cursor.execute("""
                INSERT INTO moves (accuracy, atk_num, atk_type, category, crit_rate, friend_side_effect, friend_side_effect_arg, id, max_pp, mon_id, must_hit, name, power, priority, side_effect, side_effect_arg, type, info, ordinary, EffectText)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,(accuracy, atk_num, atk_type, category, crit_rate, friend_side_effect, friend_side_effect_arg, id, max_pp, mon_id, must_hit, name, power, priority, side_effect, side_effect_arg, type, info, ordinary, EffectText)
            )
        except Exception as e:
            print(f"❌ 插入数据失败：{e}，数据内容：{i}")
    conn.commit()
db_moves()
def db_pet_skin():
    with open(data_path / "pet_skin.json", 'r', encoding='utf-8') as f:
        data = json.load(f)
    root = data["root"]


    def create_scheme_database(db_path):
        # 确保数据库所在目录存在
        db_dir = os.path.dirname(db_path)
        if db_dir and not os.path.exists(db_dir):
            os.makedirs(db_dir)

        try:
            # 连接数据库（文件不存在则自动创建）
            conn = sqlite3.connect(db_path)
            cursor = conn.cursor()

            create_table_sql = """
            CREATE TABLE IF NOT EXISTS pet_skin (
                dbid INTEGER PRIMARY KEY AUTOINCREMENT,
                Go TEXT NOT NULL,        -- 
                GoType TEXT NOT NULL,        -- 
                ID INTEGER NOT NULL,        -- 
                Jumptarget INTEGER NOT NULL,        -- 
                MonID INTEGER NOT NULL,        -- 
                Name TEXT NOT NULL,        -- 
                SkinKindID INTEGER NOT NULL,        -- 
                SkinKindLifeTime INTEGER NOT NULL,        -- 
                SkinKindSkinType INTEGER NOT NULL,        -- 
                SkinKindType INTEGER NOT NULL,        -- 
                SkinKindYear INTEGER NOT NULL,        -- 
                Type INTEGER NOT NULL  -- 
            );
            """
            cursor.execute(create_table_sql)
            conn.commit()

            print(f"✅ 数据库创建成功：{db_path}")
            print("✅ 数据表创建成功（字段结构完全匹配要求）")

        except sqlite3.Error as e:
            print(f"❌ 数据库创建失败：{e}")
        finally:
            # 确保连接关闭
            if conn:
                conn.close()

    DB_PATH = str(data_path / "pet_skin.db")  # CI 输出路径

    try:
        os.remove(DB_PATH)
    except:
        pass

    create_scheme_database(DB_PATH)

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row  # 设置为字典格式
    cursor = conn.cursor()

    for i in root:
        try:
            Go = i.get("Go", '')
            GoType = i.get("GoType", '')
            ID = i.get("ID", 0)
            Jumptarget = i.get("Jumptarget", 0)
            MonID = i.get("MonID", 0)
            Name = i.get("Name", '')
            SkinKindID = i["SkinKind"][0]["ID"]
            SkinKindLifeTime = i["SkinKind"][0]["LifeTime"]
            SkinKindSkinType = i["SkinKind"][0]["SkinType"]
            SkinKindType = i["SkinKind"][0]["Type"]
            SkinKindYear = i["SkinKind"][0]["Year"]
            Type = i.get("Type", 0)
            cursor.execute("""
                INSERT INTO pet_skin (Go, GoType, ID, Jumptarget, MonID, Name, SkinKindID, SkinKindLifeTime, SkinKindSkinType, SkinKindType, SkinKindYear, Type)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,(Go, GoType, ID, Jumptarget, MonID, Name, SkinKindID, SkinKindLifeTime, SkinKindSkinType, SkinKindType, SkinKindYear, Type)
            )
        except Exception as e:
            print(f"❌ 插入数据失败：{e}，数据内容：{i}")
    conn.commit()
db_pet_skin()
def db_pets():
    with open(data_path / "monsters.json", 'r', encoding='utf-8') as f:
        data = json.load(f)
    root = data["Monsters"]["Monster"]
    with open(data_path / "petbook.json", 'r', encoding='utf-8') as f:
        data = json.load(f)
    root1 = data["root"]["Monster"]
    book = []
    for i in root1:
        book.append(i["ID"])
    with open(data_path / "pet_advance.json", 'r', encoding='utf-8') as f:
        data = json.load(f)
    root2 = data["root"]["Task"]
    with open(data_path / "awakendetail.json", 'r', encoding='utf-8') as f:
        data = json.load(f)
    root3 = data["root"]["Task"]
    pro = []
    for i in root2:
        race = [int(n) for n in i["Advances"]["Race"]["NewRace"].split(' ')]
        pro.append([i["Advances"]["MonsterId"], race])
    for i in root3:
        race = i["Advances"]["Race"]["NewRace"]
        pro.append([i["Advances"]["MonsterId"], race])


    def create_scheme_database(db_path):
        # 确保数据库所在目录存在
        db_dir = os.path.dirname(db_path)
        if db_dir and not os.path.exists(db_dir):
            os.makedirs(db_dir)

        try:
            # 连接数据库（文件不存在则自动创建）
            conn = sqlite3.connect(db_path)
            cursor = conn.cursor()

            create_table_sql = """
            CREATE TABLE IF NOT EXISTS pets (
                dbid INTEGER PRIMARY KEY AUTOINCREMENT,
                isBook INTEGER NOT NULL,  -- 
                ID INTEGER NOT NULL,        -- 
                Name TEXT NOT NULL,  -- 
                Type INTEGER NOT NULL,  -- 
                Height TEXT NOT NULL,  -- 
                Weight TEXT NOT NULL,  -- 
                Gender INTEGER NOT NULL,  -- 
                Atk INTEGER NOT NULL,  -- 
                SpAtk INTEGER NOT NULL,  -- 
                Def INTEGER NOT NULL,  -- 
                SpDef INTEGER NOT NULL,  -- 
                Spd INTEGER NOT NULL,  -- 
                HP INTEGER NOT NULL,  -- 
                LearnableMoves TEXT NOT NULL,  -- 
                ExtraMoves TEXT NOT NULL,  -- 
                des TEXT NOT NULL,  -- 
                RealId INTEGER NOT NULL,  -- 
                NewAtk INTEGER,  -- 
                NewSpAtk INTEGER,  -- 
                NewDef INTEGER,  -- 
                NewSpDef INTEGER,  -- 
                NewSpd INTEGER,  -- 
                NewHP INTEGER  -- 
            );
            """
            cursor.execute(create_table_sql)
            conn.commit()

            print(f"✅ 数据库创建成功：{db_path}")
            print("✅ 数据表创建成功（字段结构完全匹配要求）")

        except sqlite3.Error as e:
            print(f"❌ 数据库创建失败：{e}")
        finally:
            # 确保连接关闭
            if conn:
                conn.close()

    DB_PATH = str(data_path / "pets.db")  # CI 输出路径

    try:
        os.remove(DB_PATH)
    except:
        pass

    create_scheme_database(DB_PATH)

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row  # 设置为字典格式
    cursor = conn.cursor()

    for i in root:
        try:
            isBook = 0
            ID = i.get("ID", 0)
            Name = i.get("DefName", '')
            Type = i.get("Type", 0)
            Height = ''
            Weight = ''
            Gender = i.get("Gender", 0)
            Atk = i.get("Atk", 0)
            SpAtk = i.get("SpAtk", 0)
            Def = i.get("Def", 0)
            SpDef = i.get("SpDef", 0)
            Spd = i.get("Spd", 0)
            HP = i.get("HP", 0)
            LearnableMoves = str(i.get("LearnableMoves", {}))
            ExtraMoves = str(i.get("ExtraMoves", {}))
            des = ''
            RealId = i.get("RealId", 0)
            if ID in book:
                ind = book.index(ID)
                isBook = 1
                Height = str(root1[ind].get("Height", 0))
                Weight = str(root1[ind].get("Weight", 0))
                des = root1[ind].get("Features", '')
            flag = False
            for j in pro:
                if j[0] == ID:
                    NewAtk = j[1][1]
                    NewSpAtk = j[1][3]
                    NewDef = j[1][2]
                    NewSpDef = j[1][4]
                    NewSpd = j[1][5]
                    NewHP = j[1][0]
                    cursor.execute("""
                        INSERT INTO pets (isBook, ID, Name, Type, Height, Weight, Gender, Atk, SpAtk, Def, SpDef, Spd, HP, LearnableMoves, ExtraMoves, des, RealId, NewAtk, NewSpAtk, NewDef, NewSpDef, NewSpd, NewHP)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,(isBook, ID, Name, Type, Height, Weight, Gender, Atk, SpAtk, Def, SpDef, Spd, HP, LearnableMoves, ExtraMoves, des, RealId, NewAtk, NewSpAtk, NewDef, NewSpDef, NewSpd, NewHP)
                    )
                    flag = True
                    break
            if flag:
                continue
            cursor.execute("""
                INSERT INTO pets (isBook, ID, Name, Type, Height, Weight, Gender, Atk, SpAtk, Def, SpDef, Spd, HP, LearnableMoves, ExtraMoves, des, RealId)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,(isBook, ID, Name, Type, Height, Weight, Gender, Atk, SpAtk, Def, SpDef, Spd, HP, LearnableMoves, ExtraMoves, des, RealId)
            )
        except Exception as e:
            print(f"❌ 插入数据失败：{e}，数据内容：{i}")
    conn.commit()
db_pets()
def db_pvp():
    with open(data_path / "pvp_ban.json", 'r', encoding='utf-8') as f:
        data = json.load(f)
    root1 = data["root"]
    with open(data_path / "pvp_ban_expert.json", 'r', encoding='utf-8') as f:
        data = json.load(f)
    root2 = data["root"]
    with open(data_path / "pvp_vote.json", 'r', encoding='utf-8') as f:
        data = json.load(f)
    root3 = data["root"]


    def create_scheme_database(db_path):
        # 确保数据库所在目录存在
        db_dir = os.path.dirname(db_path)
        if db_dir and not os.path.exists(db_dir):
            os.makedirs(db_dir)

        try:
            # 连接数据库（文件不存在则自动创建）
            conn = sqlite3.connect(db_path)
            cursor = conn.cursor()

            create_table_sql = """
            CREATE TABLE IF NOT EXISTS pvp_ban (
                dbid INTEGER PRIMARY KEY AUTOINCREMENT,
                id INTEGER NOT NULL,        -- 
                name TEXT NOT NULL,        -- 
                quantity INTEGER NOT NULL,        -- 
                subkey INTEGER NOT NULL,        -- 
                type INTEGER NOT NULL        -- 
            );
            """
            cursor.execute(create_table_sql)
            create_table_sql = """
            CREATE TABLE IF NOT EXISTS pvp_ban_expert (
                dbid INTEGER PRIMARY KEY AUTOINCREMENT,
                id INTEGER NOT NULL,        -- 
                name TEXT NOT NULL,        -- 
                quantity INTEGER NOT NULL,        -- 
                reward TEXT NOT NULL,        -- 
                seasonopen INTEGER NOT NULL,        -- 
                subkey_month INTEGER NOT NULL,        -- 
                subkey_total INTEGER NOT NULL,        -- 
                type INTEGER NOT NULL        -- 
            );
            """
            cursor.execute(create_table_sql)
            create_table_sql = """
            CREATE TABLE IF NOT EXISTS pvp_vote (
                dbid INTEGER PRIMARY KEY AUTOINCREMENT,
                id INTEGER NOT NULL,        -- 
                name TEXT NOT NULL,        -- 
                number INTEGER NOT NULL,        -- 
                oldresult TEXT NOT NULL,        -- 
                ranklimit1 INTEGER NOT NULL,        -- 
                ranklimit2 INTEGER NOT NULL,        -- 
                result TEXT NOT NULL,        -- 
                subkey INTEGER NOT NULL,        -- 
                time1 INTEGER NOT NULL,        -- 
                time2 INTEGER NOT NULL,        -- 
                type INTEGER NOT NULL        -- 
            );
            """
            cursor.execute(create_table_sql)
            conn.commit()

            print(f"✅ 数据库创建成功：{db_path}")
            print("✅ 数据表创建成功（字段结构完全匹配要求）")

        except sqlite3.Error as e:
            print(f"❌ 数据库创建失败：{e}")
        finally:
            # 确保连接关闭
            if conn:
                conn.close()

    DB_PATH = str(data_path / "pvp.db")  # CI 输出路径

    try:
        os.remove(DB_PATH)
    except:
        pass

    create_scheme_database(DB_PATH)

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row  # 设置为字典格式
    cursor = conn.cursor()

    for i in root1:
        try:
            id = i.get("id", 0)
            name = str(i.get("name", []))
            quantity = i.get("quantity", 0)
            subkey = i.get("subkey", 0)
            type = i.get("type", 0)
            cursor.execute("""
                INSERT INTO pvp_ban (id, name, quantity, subkey, type)
                VALUES (?, ?, ?, ?, ?)
                """, (id, name, quantity, subkey, type)
            )
        except Exception as e:
            print(f"❌ 插入数据失败：{e}，数据内容：{i}")
    for i in root2:
        try:
            id = i.get("id", 0)
            name = i.get("name", "")
            quantity = i.get("quantity", 0)
            reward = i.get("reward", "")
            seasonopen = i.get("seasonopen", 0)
            subkey_month = i.get("subkey_month", 0)
            subkey_total = i.get("subkey_total", 0)
            type = i.get("type", 0)
            cursor.execute("""
                INSERT INTO pvp_ban_expert (id, name, quantity, reward, seasonopen, subkey_month, subkey_total, type)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """, (id, name, quantity, reward, seasonopen, subkey_month, subkey_total, type)
            )
        except Exception as e:
            print(f"❌ 插入数据失败：{e}，数据内容：{i}")
    for i in root3:
        try:
            id = i.get("id", 0)
            name = i.get("name", "")
            number = i.get("number", 0)
            oldresult = i.get("oldresult", "")
            ranklimit1 = i.get("ranklimit1", 0)
            ranklimit2 = i.get("ranklimit2", 0)
            result = i.get("result", "")
            subkey = i.get("subkey", 0)
            time1 = i.get("time1", 0)
            time2 = i.get("time2", 0)
            type = i.get("type", 0)
            cursor.execute("""
                INSERT INTO pvp_vote (id, name, number, oldresult, ranklimit1, ranklimit2, result, subkey, time1, time2, type)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (id, name, number, oldresult, ranklimit1, ranklimit2, result, subkey, time1, time2, type)
            )
        except Exception as e:
            print(f"❌ 插入数据失败：{e}，数据内容：{i}")
    conn.commit()
db_pvp()
def db_rich_text_tree():
    with open(data_path / "rich_text_tree.json", 'r', encoding='utf-8') as f:
        data = json.load(f)
    root = data


    def create_scheme_database(db_path):
        # 确保数据库所在目录存在
        db_dir = os.path.dirname(db_path)
        if db_dir and not os.path.exists(db_dir):
            os.makedirs(db_dir)

        try:
            # 连接数据库（文件不存在则自动创建）
            conn = sqlite3.connect(db_path)
            cursor = conn.cursor()

            create_table_sql = """
            CREATE TABLE IF NOT EXISTS rich_text_tree (
                dbid INTEGER PRIMARY KEY AUTOINCREMENT,
                id INTEGER NOT NULL,        -- 
                pve INTEGER NOT NULL,  -- 
                text TEXT NOT NULL  -- 
            );
            """
            cursor.execute(create_table_sql)
            conn.commit()

            print(f"✅ 数据库创建成功：{db_path}")
            print("✅ 数据表创建成功（字段结构完全匹配要求）")

        except sqlite3.Error as e:
            print(f"❌ 数据库创建失败：{e}")
        finally:
            # 确保连接关闭
            if conn:
                conn.close()

    DB_PATH = str(data_path / "rich_text_tree.db")  # CI 输出路径

    try:
        os.remove(DB_PATH)
    except:
        pass

    create_scheme_database(DB_PATH)

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row  # 设置为字典格式
    cursor = conn.cursor()

    for i in root:
        try:
            id = i.get('id', 0)
            pve = i.get('pve', 0)
            text = str(i.get('text', []))
            cursor.execute("""
                INSERT INTO rich_text_tree (id, pve, text)
                VALUES (?, ?, ?)
                """, (id, pve, text)
            )
        except Exception as e:
            print(f"❌ 插入数据失败：{e}，数据内容：{i}")
    conn.commit()
db_rich_text_tree()
def db_skill_effect():
    with open(data_path / "skill_effect.json", 'r', encoding='utf-8') as f:
        data = json.load(f)
    root = data["data"]


    def create_scheme_database(db_path):
        # 确保数据库所在目录存在
        db_dir = os.path.dirname(db_path)
        if db_dir and not os.path.exists(db_dir):
            os.makedirs(db_dir)

        try:
            # 连接数据库（文件不存在则自动创建）
            conn = sqlite3.connect(db_path)
            cursor = conn.cursor()

            create_table_sql = """
            CREATE TABLE IF NOT EXISTS skill_effect (
                dbid INTEGER PRIMARY KEY AUTOINCREMENT,
                Bosseffective INTEGER NOT NULL,        -- 
                argsNum INTEGER NOT NULL,  -- 
                formattingAdjustment TEXT NOT NULL,        -- 
                id INTEGER NOT NULL,  -- 
                ifTextItalic TEXT NOT NULL,        -- 
                info TEXT NOT NULL,  -- 
                isif INTEGER NOT NULL,        -- 
                tagA TEXT NOT NULL,  -- 
                tagAboss INTEGER NOT NULL,        -- 
                tagB TEXT NOT NULL,  -- 
                tagBboss INTEGER NOT NULL,        -- 
                tagC TEXT NOT NULL,  -- 
                tagCboss INTEGER NOT NULL  -- 
            );
            """
            cursor.execute(create_table_sql)
            conn.commit()

            print(f"✅ 数据库创建成功：{db_path}")
            print("✅ 数据表创建成功（字段结构完全匹配要求）")

        except sqlite3.Error as e:
            print(f"❌ 数据库创建失败：{e}")
        finally:
            # 确保连接关闭
            if conn:
                conn.close()

    DB_PATH = str(data_path / "skill_effect.db")  # CI 输出路径

    try:
        os.remove(DB_PATH)
    except:
        pass

    create_scheme_database(DB_PATH)

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row  # 设置为字典格式
    cursor = conn.cursor()

    for i in root:
        try:
            Bosseffective = i.get("Bosseffective", 0)
            argsNum = i.get("argsNum", 0)
            formattingAdjustment = i.get("formattingAdjustment", "")
            id = i.get("id", 0)
            ifTextItalic = i.get("ifTextItalic", "")
            info = i.get("info", "")
            isif = i.get("isif", 0)
            tagA = i.get("tagA", "")
            tagAboss = i.get("tagAboss", 0)
            tagB = i.get("tagB", "")
            tagBboss = i.get("tagBboss", 0)
            tagC = i.get("tagC", "")
            tagCboss = i.get("tagCboss", 0)
            cursor.execute("""
                INSERT INTO skill_effect (Bosseffective, argsNum, formattingAdjustment, id, ifTextItalic, info, isif, tagA, tagAboss, tagB, tagBboss, tagC, tagCboss)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (Bosseffective, argsNum, formattingAdjustment, id, ifTextItalic, info, isif, tagA, tagAboss, tagB, tagBboss, tagC, tagCboss)
            )
        except Exception as e:
            print(f"❌ 插入数据失败：{e}，数据内容：{i}")
    conn.commit()
db_skill_effect()
def db_skillTypes():
    with open(data_path / "skillTypes.json", 'r', encoding='utf-8') as f:
        data = json.load(f)
    root = data["root"]


    def create_scheme_database(db_path):
        # 确保数据库所在目录存在
        db_dir = os.path.dirname(db_path)
        if db_dir and not os.path.exists(db_dir):
            os.makedirs(db_dir)

        try:
            # 连接数据库（文件不存在则自动创建）
            conn = sqlite3.connect(db_path)
            cursor = conn.cursor()

            create_table_sql = """
            CREATE TABLE IF NOT EXISTS skillTypes (
                dbid INTEGER PRIMARY KEY AUTOINCREMENT,
                att TEXT NOT NULL,        -- 
                cn TEXT NOT NULL,        -- 
                en TEXT NOT NULL,        -- 
                id INTEGER NOT NULL,        -- 
                is_dou INTEGER NOT NULL  -- 
            );
            """
            cursor.execute(create_table_sql)
            conn.commit()

            print(f"✅ 数据库创建成功：{db_path}")
            print("✅ 数据表创建成功（字段结构完全匹配要求）")

        except sqlite3.Error as e:
            print(f"❌ 数据库创建失败：{e}")
        finally:
            # 确保连接关闭
            if conn:
                conn.close()

    DB_PATH = str(data_path / "skillTypes.db")  # CI 输出路径

    try:
        os.remove(DB_PATH)
    except:
        pass

    create_scheme_database(DB_PATH)

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row  # 设置为字典格式
    cursor = conn.cursor()

    for i in root:
        try:
            att = i.get("att", "")
            cn = i.get("cn", "")
            en = str(i.get("en", []))
            id = i.get("id", 0)
            is_dou = i.get("is_dou", 0)
            cursor.execute("""
                INSERT INTO skillTypes (att, cn, en, id, is_dou)
                VALUES (?, ?, ?, ?, ?)
                """, (att, cn, en, id, is_dou)
            )
        except Exception as e:
            print(f"❌ 插入数据失败：{e}，数据内容：{i}")
    conn.commit()
db_skillTypes()
def db_sp_hide_moves():
    with open(data_path / "sp_hide_moves.json", 'r', encoding='utf-8') as f:
        data = json.load(f)
    root1 = data["root"]["ShowMoves"]
    root2 = data["root"]["SpMoves"]


    def create_scheme_database(db_path):
        # 确保数据库所在目录存在
        db_dir = os.path.dirname(db_path)
        if db_dir and not os.path.exists(db_dir):
            os.makedirs(db_dir)

        try:
            # 连接数据库（文件不存在则自动创建）
            conn = sqlite3.connect(db_path)
            cursor = conn.cursor()

            create_table_sql = """
            CREATE TABLE IF NOT EXISTS ShowMoves (
                dbid INTEGER PRIMARY KEY AUTOINCREMENT,
                id INTEGER NOT NULL,        -- 
                item INTEGER NOT NULL,        -- 
                itemname TEXT NOT NULL,        -- 
                itemnumber INTEGER NOT NULL,        -- 
                monster INTEGER NOT NULL,        -- 
                moves INTEGER NOT NULL,        -- 
                movesname TEXT NOT NULL,        -- 
                movetype INTEGER NOT NULL        -- 
            );
            """
            cursor.execute(create_table_sql)
            create_table_sql = """
            CREATE TABLE IF NOT EXISTS SpMoves (
                dbid INTEGER PRIMARY KEY AUTOINCREMENT,
                id INTEGER NOT NULL,        -- 
                item INTEGER NOT NULL,        -- 
                itemname TEXT NOT NULL,        -- 
                itemnumber INTEGER NOT NULL,        -- 
                monster INTEGER NOT NULL,        -- 
                moves INTEGER NOT NULL,        -- 
                movesname TEXT NOT NULL,        -- 
                movetype INTEGER NOT NULL        -- 
            );
            """
            cursor.execute(create_table_sql)
            conn.commit()

            print(f"✅ 数据库创建成功：{db_path}")
            print("✅ 数据表创建成功（字段结构完全匹配要求）")

        except sqlite3.Error as e:
            print(f"❌ 数据库创建失败：{e}")
        finally:
            # 确保连接关闭
            if conn:
                conn.close()

    DB_PATH = str(data_path / "sp_hide_moves.db")  # CI 输出路径

    try:
        os.remove(DB_PATH)
    except:
        pass

    create_scheme_database(DB_PATH)

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row  # 设置为字典格式
    cursor = conn.cursor()

    for i in root1:
        try:
            id = i.get("id", 0)
            item = i.get("item", 0)
            itemname = i.get("itemname", "")
            itemnumber = i.get("itemnumber", 0)
            monster = i.get("monster", 0)
            moves = i.get("moves", 0)
            movesname = i.get("movesname", "")
            movetype = i.get("movetype", 0)
            cursor.execute("""
                INSERT INTO ShowMoves (id, item, itemname, itemnumber, monster, moves, movesname, movetype)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """, (id, item, itemname, itemnumber, monster, moves, movesname, movetype)
            )
        except Exception as e:
            print(f"❌ 插入数据失败：{e}，数据内容：{i}")
    for i in root2:
        try:
            id = i.get("id", 0)
            item = i.get("item", 0)
            itemname = i.get("itemname", "")
            itemnumber = i.get("itemnumber", 0)
            monster = i.get("monster", 0)
            moves = i.get("moves", 0)
            movesname = i.get("movesname", "")
            movetype = i.get("movetype", 0)
            cursor.execute("""
                INSERT INTO SpMoves (id, item, itemname, itemnumber, monster, moves, movesname, movetype)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """, (id, item, itemname, itemnumber, monster, moves, movesname, movetype)
            )
        except Exception as e:
            print(f"❌ 插入数据失败：{e}，数据内容：{i}")
    conn.commit()
db_sp_hide_moves()





with open(data_path/'version1.txt', "w", encoding="utf-8") as file:
    file.write(version1)
with open(data_path/'version2.txt', "w", encoding="utf-8") as file:
    file.write(version2)
print("数据更新完成！", version1, version2)