"""从 fightResource/pet/swf 导出精灵战斗动画（pet.png + 帧标签 GIF）。"""

from __future__ import annotations

import json
import os
import re
import shutil
import stat
import struct
import subprocess
import tempfile
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path

import requests
from PIL import Image

PET_ANIM_MAX_ZOOM = 1.0
PET_ANIM_PNG_ZOOM = 2
PET_ANIM_TARGET_GIF_BYTES = 2 * 1024 * 1024
PET_ANIM_BYTES_PER_PIXEL_FRAME = 15_208_092 / (3154 * 2574 * 91)
PET_ANIM_MIN_GIF_ZOOM = 0.05
PET_ANIM_PROBE_TIMEOUT_SEC = 30
PET_ANIM_PROBE_FALLBACK_ASSUMED_SIZE = (1920, 1920)
PET_ANIM_LABEL_NAMES = {
	"attack": "物攻",
	"sa": "特攻",
	"cp": "属性",
	"hited": "受击",
	"appear": "出场",
	"transform": "变身",
}


def pet_anim_dir_nonempty(path: Path) -> bool:
	"""目录存在且至少有一项内容时视为已导出。"""
	if not path.is_dir():
		return False
	try:
		return next(path.iterdir(), None) is not None
	except OSError:
		return False


def pet_anim_export_complete(path: Path) -> bool:
	"""目录已有完整导出或 404 占位时视为无需再导。"""
	if not path.is_dir():
		return False
	try:
		files = [p for p in path.iterdir() if p.is_file()]
	except OSError:
		return False
	if not files:
		return False
	names = {p.name for p in files}
	if names <= {".gitkeep"}:
		return True
	if ".no_labels" in names:
		return True
	if "pet.png" not in names:
		return False
	gif_count = sum(1 for n in names if n.endswith(".gif"))
	# 常见 5 标签精灵；少于 4 个 GIF 视为上次导出中断的半成品
	return gif_count >= 4


def _pet_anim_chmod_writable(path: Path) -> None:
	try:
		os.chmod(path, stat.S_IWRITE)
	except OSError:
		pass


def _pet_anim_on_rmtree_error(func, path: str, _exc_info) -> None:
	_pet_anim_chmod_writable(Path(path))
	func(path)


def _pet_anim_remove_path(path: Path, retries: int = 10) -> bool:
	if not path.exists():
		return True
	for i in range(retries):
		try:
			if path.is_dir() and not path.is_symlink():
				for child in sorted(path.rglob("*"), key=lambda p: len(p.parts), reverse=True):
					if child.is_file() or child.is_symlink():
						_pet_anim_chmod_writable(child)
						child.unlink(missing_ok=True)
					elif child.is_dir():
						try:
							child.rmdir()
						except OSError:
							pass
			if path.is_file() or path.is_symlink():
				_pet_anim_chmod_writable(path)
				path.unlink(missing_ok=True)
			elif path.is_dir():
				shutil.rmtree(path, onerror=_pet_anim_on_rmtree_error)
			return not path.exists()
		except OSError:
			time.sleep(0.3 * (i + 1))
	return not path.exists()


def _pet_anim_terminate_proc(proc: subprocess.Popen) -> None:
	if proc.poll() is not None:
		return
	if os.name == "nt":
		subprocess.run(
			["taskkill", "/F", "/T", "/PID", str(proc.pid)],
			stdout=subprocess.DEVNULL,
			stderr=subprocess.DEVNULL,
		)
	else:
		proc.kill()
	try:
		proc.wait(timeout=15)
	except subprocess.TimeoutExpired:
		pass
	time.sleep(0.5)


def _pet_anim_reset_export_dir(path: Path) -> None:
	if path.is_file():
		path.unlink(missing_ok=True)
	elif path.is_dir():
		for entry in path.iterdir():
			try:
				if entry.is_dir():
					shutil.rmtree(entry, ignore_errors=True)
				else:
					entry.unlink(missing_ok=True)
			except OSError:
				pass
	path.mkdir(parents=True, exist_ok=True)


def _pet_anim_read_png_size(path: Path) -> tuple[int, int] | None:
	try:
		with path.open("rb") as f:
			header = f.read(24)
		if len(header) < 24 or header[:8] != b"\x89PNG\r\n\x1a\n":
			return None
		width, height = struct.unpack(">II", header[16:24])
		return width, height
	except OSError:
		return None


def _pet_anim_png_write_stable(path: Path, min_bytes: int = 256, stable_sec: float = 0.4) -> bool:
	if not path.exists() or path.stat().st_size < min_bytes:
		return False
	size = path.stat().st_size
	time.sleep(stable_sec)
	return path.exists() and path.stat().st_size == size


@dataclass
class _PetAnimGifJob:
	label: str
	out_name: str
	character_id: int
	class_name: str
	frame_count: int
	first_size: tuple[int, int] | None
	zoom: float


class PetAnimExporter:
	"""从 fightResource/pet/swf 导出 pet.png 与帧标签 GIF。"""

	def __init__(self, pet_id: int, work_dir: Path, ffdec_jar: Path):
		self.pet_id = pet_id
		self.work_dir = work_dir
		self.ffdec_jar = ffdec_jar
		self.tmp_dir = Path(tempfile.gettempdir()) / f"seer_export_{pet_id}"
		self.swf_path = self.tmp_dir / f"{pet_id}.swf"
		self.xml_path = self.tmp_dir / f"{pet_id}.xml"
		self.symbol_dir = self.tmp_dir / "symbol_class"
		self.tmp_export = self.tmp_dir / "export"
		self.swf_url = f"https://seer.61.com/resource/fightResource/pet/swf/{pet_id}.swf"

	def _run_ffdec(self, args: list[str], cwd: Path, timeout: int = 300) -> subprocess.CompletedProcess:
		last_output = ""
		proc: subprocess.CompletedProcess | None = None
		for attempt in range(3):
			proc = subprocess.run(
				["java", "-jar", str(self.ffdec_jar), "-onerror", "ignore", *args],
				cwd=cwd,
				capture_output=True,
				timeout=timeout,
				encoding="utf-8",
				errors="replace",
			)
			if proc.returncode == 0:
				return proc
			last_output = ((proc.stderr or "") + "\n" + (proc.stdout or "")).strip()[-1500:]
			time.sleep(0.5 * (attempt + 1))
		err = RuntimeError(
			f"FFDec 退出码 {proc.returncode}"
			+ (f": {last_output}" if last_output else "")
		)
		err.returncode = proc.returncode  # type: ignore[attr-defined]
		raise err

	def _export_symbol_classes(self) -> dict[int, str]:
		_pet_anim_reset_export_dir(self.symbol_dir)
		try:
			self._run_ffdec(
				["-export", "symbolClass", str(self.symbol_dir.resolve()), self.swf_path.name],
				cwd=self.tmp_dir,
			)
		except RuntimeError:
			if not list(self.symbol_dir.rglob("*.csv")):
				raise
		return self._parse_symbol_classes()

	def _download_swf(self) -> None:
		self.tmp_dir.mkdir(parents=True, exist_ok=True)
		r: requests.Response | None = None
		for attempt in range(2):
			r = requests.get(self.swf_url, timeout=30)
			if r.status_code != 404:
				break
			if attempt == 0:
				time.sleep(1)
		if r is not None and r.status_code == 404:
			self.work_dir.mkdir(parents=True, exist_ok=True)
			(self.work_dir / ".gitkeep").touch()
			print(f"[SKIP] SWF 不存在（404×2），已创建 .gitkeep: {self.work_dir}")
			raise FileNotFoundError(f"SWF 不存在（404×2）: {self.swf_url}")
		r.raise_for_status()
		self.swf_path.write_bytes(r.content)
		if self.swf_path.stat().st_size < 64:
			raise ValueError(f"SWF 文件过小，可能下载不完整: {self.swf_path}")
		print(f"[OK] 下载完成: {self.swf_path} ({len(r.content)} bytes)")

	def _parse_symbol_classes(self) -> dict[int, str]:
		mapping: dict[int, str] = {}
		csv_files = list(self.symbol_dir.rglob("*.csv"))
		if not csv_files:
			raise FileNotFoundError(f"未找到 symbolClass CSV: {self.symbol_dir}")
		for csv_file in csv_files:
			for line in csv_file.read_text(encoding="utf-8", errors="ignore").splitlines():
				line = line.strip()
				if not line or ";" not in line:
					continue
				cid_str, class_name = line.split(";", 1)
				class_name = class_name.strip().strip('"')
				try:
					mapping[int(cid_str.strip())] = class_name
				except ValueError:
					continue
		return mapping

	@staticmethod
	def _find_pet_id(symbol_map: dict[int, str]) -> int:
		for cid, name in symbol_map.items():
			if name == "pet":
				return cid
		raise ValueError("symbolClass 中未找到 pet")

	def _swf_to_xml(self) -> None:
		self._run_ffdec(["-swf2xml", self.swf_path.name, self.xml_path.name], cwd=self.tmp_dir, timeout=300)

	def _parse_pet_label_clips(self, pet_sprite_id: int) -> dict[str, tuple[int, int, int]]:
		root = ET.parse(self.xml_path).getroot()
		for sprite in root.iter("item"):
			if sprite.get("type") != "DefineSpriteTag" or sprite.get("spriteId") != str(pet_sprite_id):
				continue
			frame = 0
			current_label: str | None = None
			current_char: int | None = None
			label_start: int | None = None
			segments: dict[str, tuple[int, int, int]] = {}
			subtags = sprite.find("subTags")
			if subtags is None:
				break
			for sub in subtags.findall("item"):
				tag_type = sub.get("type")
				if tag_type == "FrameLabelTag":
					if current_label is not None and current_char is not None and label_start is not None:
						segments[current_label] = (label_start, frame, current_char)
					current_label = sub.get("name")
					label_start = frame + 1
					current_char = None
				elif tag_type == "PlaceObject2Tag" and current_char is None:
					if sub.get("placeFlagHasCharacter") == "true":
						current_char = int(sub.get("characterId"))
				elif tag_type == "ShowFrameTag":
					frame += 1
			if current_label is not None and current_char is not None and label_start is not None:
				segments[current_label] = (label_start, frame, current_char)
			break
		return segments

	def _load_sprite_frame_counts(self) -> dict[int, int]:
		counts: dict[int, int] = {}
		for item in ET.parse(self.xml_path).getroot().iter("item"):
			if item.get("type") == "DefineSpriteTag" and item.get("spriteId"):
				counts[int(item.get("spriteId"))] = int(item.get("frameCount", 0))
		return counts

	def _export_sprite_first_png(
		self,
		character_id: int,
		zoom: float,
		work: Path,
		timeout: int = PET_ANIM_PROBE_TIMEOUT_SEC,
	) -> Path | None:
		_pet_anim_reset_export_dir(work)
		proc = subprocess.Popen(
			[
				"java", "-jar", str(self.ffdec_jar),
				"-onerror", "ignore",
				"-zoom", str(zoom),
				"-selectid", str(character_id),
				"-format", "sprite:png",
				"-export", "sprite",
				str(work.resolve()),
				self.swf_path.name,
			],
			cwd=self.tmp_dir,
			stdout=subprocess.DEVNULL,
			stderr=subprocess.DEVNULL,
		)
		first_png: Path | None = None
		deadline = time.monotonic() + timeout
		try:
			while time.monotonic() < deadline:
				pngs = sorted(work.rglob("*.png"), key=lambda p: p.name)
				if pngs and _pet_anim_png_write_stable(pngs[0]):
					first_png = pngs[0]
					break
				if proc.poll() is not None:
					pngs = sorted(work.rglob("*.png"), key=lambda p: p.name)
					if pngs and pngs[0].stat().st_size > 64:
						time.sleep(0.6)
						first_png = pngs[0]
					break
				time.sleep(0.2)
			if proc.poll() is None:
				_pet_anim_terminate_proc(proc)
			else:
				time.sleep(0.2)
		except Exception:
			if proc.poll() is None:
				_pet_anim_terminate_proc(proc)
			raise
		return first_png

	def _probe_sprite_size(self, character_id: int) -> tuple[int, int] | None:
		"""探测 sprite 首帧尺寸；失败时降低 zoom 重试，并按 zoom 还原为原始像素尺寸。"""
		for zoom in (1.0, 0.5, 0.25):
			timeout = 60 if zoom >= 1.0 else 45
			png = self._export_sprite_first_png(character_id, zoom, self.tmp_export, timeout=timeout)
			size = _pet_anim_read_png_size(png) if png is not None else None
			if size and size[0] > 0 and size[1] > 0:
				if zoom != 1.0:
					size = (max(1, int(size[0] / zoom)), max(1, int(size[1] / zoom)))
				return size
		return None

	def _probe_all_sprite_sizes(self, character_ids: list[int]) -> dict[int, tuple[int, int] | None]:
		unique_ids = list(dict.fromkeys(character_ids))
		cache: dict[int, tuple[int, int] | None] = {}
		total = len(unique_ids)
		for i, cid in enumerate(unique_ids, 1):
			print(f"[探测 {i}/{total}] sprite {cid} ...", flush=True)
			if cid in cache:
				continue
			cache[cid] = self._probe_sprite_size(cid)
		return cache

	@staticmethod
	def _calc_gif_zoom(width: int, height: int, frame_count: int) -> float:
		if width <= 0 or height <= 0 or frame_count <= 0:
			return PET_ANIM_MAX_ZOOM
		est_at_max = width * height * frame_count * PET_ANIM_BYTES_PER_PIXEL_FRAME
		if est_at_max <= PET_ANIM_TARGET_GIF_BYTES:
			return PET_ANIM_MAX_ZOOM
		ratio = PET_ANIM_TARGET_GIF_BYTES / est_at_max
		zoom = (ratio ** 0.5) * PET_ANIM_MAX_ZOOM
		return max(PET_ANIM_MIN_GIF_ZOOM, min(PET_ANIM_MAX_ZOOM, round(zoom, 2)))

	@staticmethod
	def _calc_gif_zoom_fallback(frame_count: int) -> float:
		w, h = PET_ANIM_PROBE_FALLBACK_ASSUMED_SIZE
		return PetAnimExporter._calc_gif_zoom(w, h, frame_count)

	@staticmethod
	def _map_label_to_export_name(label: str) -> str | None:
		if label in PET_ANIM_LABEL_NAMES:
			return PET_ANIM_LABEL_NAMES[label]
		add_match = re.fullmatch(r"add(\d+)", label)
		if add_match:
			return f"额外_{add_match.group(1)}"
		moves_match = re.fullmatch(r"moves_(.+)", label)
		if moves_match:
			return f"技能_{moves_match.group(1)}"
		return None

	@staticmethod
	def _build_export_names(labels: list[str]) -> dict[str, str]:
		names: dict[str, str] = {}
		special_labels: list[str] = []
		for label in labels:
			mapped = PetAnimExporter._map_label_to_export_name(label)
			if mapped is not None:
				names[label] = mapped
			else:
				special_labels.append(label)
		if len(special_labels) == 1:
			names[special_labels[0]] = "特殊"
		elif len(special_labels) > 1:
			for i, label in enumerate(special_labels, 1):
				names[label] = f"特殊_{i}"
		return names

	@staticmethod
	def _save_transparent_png_crop(src: Path, dst: Path) -> tuple[int, int]:
		with Image.open(src) as im:
			im = im.convert("RGBA")
			bbox = im.getchannel("A").getbbox()
			if bbox:
				im = im.crop(bbox)
			im.save(dst)
			return im.size

	def _export_pet_first_frame_png(self, pet_sprite_id: int) -> Path | None:
		png_path = self._export_sprite_first_png(pet_sprite_id, PET_ANIM_PNG_ZOOM, self.tmp_export, timeout=120)
		if png_path is None:
			print(f"[FAIL] pet 第 1 帧未导出 PNG (id={pet_sprite_id})")
			return None
		self.work_dir.mkdir(parents=True, exist_ok=True)
		out_path = self.work_dir / "pet.png"
		size = self._save_transparent_png_crop(png_path, out_path)
		print(f"[OK] pet 第 1 帧(sprite 透明+裁剪 {size[0]}x{size[1]}) -> {out_path}")
		return out_path

	def _export_label_gif(self, job: _PetAnimGifJob) -> Path | None:
		safe_name = re.sub(r'[<>:"/\\|?*]', "_", job.out_name)
		zoom = job.zoom
		last_error: RuntimeError | None = None
		for attempt in range(4):
			if attempt > 0:
				print(
					f"[WARN] {job.label} 导出失败，zoom {job.zoom} -> {zoom} 重试 ({attempt + 1}/4)",
					flush=True,
				)
			try:
				return self._export_label_gif_at_zoom(job, safe_name, zoom)
			except RuntimeError as exc:
				last_error = exc
				rc = getattr(exc, "returncode", None)
				msg = str(exc)
				killed = rc in (-9, 137) or "退出码 -9" in msg or "退出码 137" in msg
				if not killed or attempt >= 3:
					raise
				zoom = max(PET_ANIM_MIN_GIF_ZOOM, round(zoom * 0.5, 2))
		if last_error:
			raise last_error
		return None

	def _export_label_gif_at_zoom(self, job: _PetAnimGifJob, safe_name: str, zoom: float) -> Path | None:
		_pet_anim_reset_export_dir(self.tmp_export)
		size_hint = f"{job.first_size[0]}x{job.first_size[1]}" if job.first_size else "?"
		print(
			f"[...] 导出 {job.label} -> {job.out_name} "
			f"(子动画 {job.frame_count} 帧, 首帧 {size_hint}, zoom={zoom})",
			flush=True,
		)
		self._run_ffdec(
			[
				"-zoom", str(zoom),
				"-selectid", str(job.character_id),
				"-format", "sprite:gif",
				"-export", "sprite",
				str(self.tmp_export.resolve()),
				self.swf_path.name,
			],
			cwd=self.tmp_dir,
			timeout=600,
		)
		gif_files = list(self.tmp_export.rglob("*.gif"))
		if not gif_files:
			print(f"[FAIL] {job.label} -> {job.out_name} (id={job.character_id}, {job.class_name}) 未导出 GIF")
			return None
		self.work_dir.mkdir(parents=True, exist_ok=True)
		out_path = self.work_dir / f"{safe_name}.gif"
		shutil.copy2(gif_files[0], out_path)
		_pet_anim_remove_path(self.tmp_export)
		gif_bytes = out_path.stat().st_size
		print(f"[OK] {job.label} -> {out_path} (id={job.character_id}, zoom={zoom}, {gif_bytes // 1024}KB)")
		return out_path

	def _init_workspace(self) -> None:
		self.work_dir.mkdir(parents=True, exist_ok=True)
		if self.tmp_dir.exists():
			_pet_anim_remove_path(self.tmp_dir)
			time.sleep(0.3)
		self.tmp_dir.mkdir(parents=True)
		for name in ("tmp", f"{self.pet_id}.swf", f"{self.pet_id}.xml", "symbol_class", "gif"):
			_pet_anim_remove_path(self.work_dir / name)
		for entry in list(self.work_dir.iterdir()):
			if entry.is_dir() and entry.name.startswith("tmp_"):
				_pet_anim_remove_path(entry)

	def _cleanup_workspace(self) -> None:
		if self.tmp_dir.exists() and not _pet_anim_remove_path(self.tmp_dir):
			print(f"[WARN] 无法删除临时目录 {self.tmp_dir}，可能被 Java 占用")
		for name in ("tmp", f"{self.pet_id}.swf", f"{self.pet_id}.xml", "symbol_class", "gif"):
			_pet_anim_remove_path(self.work_dir / name)
		if not self.work_dir.exists():
			return
		for entry in list(self.work_dir.iterdir()):
			if entry.is_dir():
				_pet_anim_remove_path(entry)
			elif entry.suffix.lower() != ".gif" and entry.name not in ("pet.png", ".gitkeep", ".no_labels"):
				entry.unlink(missing_ok=True)

	def _build_gif_jobs(
		self,
		label_clips: dict[str, tuple[int, int, int]],
		export_names: dict[str, str],
		symbol_map: dict[int, str],
		sprite_frame_counts: dict[int, int],
		size_cache: dict[int, tuple[int, int] | None],
	) -> list[_PetAnimGifJob]:
		jobs: list[_PetAnimGifJob] = []
		for label, (_, _, character_id) in label_clips.items():
			out_name = export_names[label]
			class_name = symbol_map.get(character_id, f"DefineSprite_{character_id}")
			frame_count = sprite_frame_counts.get(character_id, 1)
			first_size = size_cache.get(character_id)
			if first_size:
				zoom = self._calc_gif_zoom(first_size[0], first_size[1], frame_count)
			else:
				print(f"[WARN] {label} 首帧探测失败，仅按帧数估 zoom", flush=True)
				zoom = self._calc_gif_zoom_fallback(frame_count)
			jobs.append(_PetAnimGifJob(label, out_name, character_id, class_name, frame_count, first_size, zoom))
		return jobs

	def export(self) -> None:
		if not self.ffdec_jar.exists():
			raise FileNotFoundError(f"未找到 ffdec.jar: {self.ffdec_jar}")
		original_cwd = os.getcwd()
		export_ok = False
		try:
			self._init_workspace()
			self._download_swf()
			symbol_map = self._export_symbol_classes()
			pet_sprite_id = self._find_pet_id(symbol_map)
			print(f"pet sprite id={pet_sprite_id}")
			self._export_pet_first_frame_png(pet_sprite_id)
			self._swf_to_xml()
			sprite_frame_counts = self._load_sprite_frame_counts()
			label_clips = self._parse_pet_label_clips(pet_sprite_id)
			if not label_clips:
				print("[FAIL] pet 时间轴中未找到任何帧标签，写入 .no_labels 占位")
				self.work_dir.mkdir(parents=True, exist_ok=True)
				(self.work_dir / ".no_labels").touch()
				export_ok = True
				return
			export_names = self._build_export_names(list(label_clips.keys()))
			print(
				f"找到 {len(label_clips)} 个帧标签 "
				f"(GIF 目标约 {PET_ANIM_TARGET_GIF_BYTES // 1024 // 1024}MB, 按首帧+帧数动态 zoom)",
			)
			for label, (_, _, cid) in label_clips.items():
				fc = sprite_frame_counts.get(cid, "?")
				print(f"  {label} -> {export_names[label]} (sprite {cid}, {fc} 帧)")
			character_ids = [cid for _, _, cid in label_clips.values()]
			print("--- 阶段 1/3: 探测子动画首帧尺寸 ---")
			size_cache = self._probe_all_sprite_sizes(character_ids)
			print("--- 阶段 2/3: 计算各标签 zoom ---")
			gif_jobs = self._build_gif_jobs(
				label_clips, export_names, symbol_map, sprite_frame_counts, size_cache,
			)
			for job in gif_jobs:
				sz = f"{job.first_size[0]}x{job.first_size[1]}" if job.first_size else "?"
				print(f"  {job.label} -> {job.out_name}  首帧 {sz}  zoom={job.zoom}")
			print("--- 阶段 3/3: 导出 GIF ---")
			exported: list[Path] = []
			total = len(gif_jobs)
			for i, job in enumerate(gif_jobs, 1):
				print(f"[GIF {i}/{total}]", end=" ", flush=True)
				result = self._export_label_gif(job)
				if result:
					exported.append(result)
			if not exported:
				print("[FAIL] 未成功导出任何 GIF")
			elif len(exported) < len(gif_jobs):
				raise RuntimeError(
					f"GIF 导出不完整：{len(exported)}/{len(gif_jobs)}，已中止以免残留半成品"
				)
			else:
				export_ok = True
				print(f"\n完成，共导出 {len(exported)} 个 GIF -> {self.work_dir}")
		except Exception:
			if not export_ok and self.work_dir.exists():
				print(f"[WARN] 导出未完成，清理目录以便下次重试: {self.work_dir}", flush=True)
				_pet_anim_remove_path(self.work_dir)
			raise
		finally:
			try:
				os.chdir(original_cwd)
			except OSError:
				pass
			self._cleanup_workspace()


def export_pet_animations(pet_id: int, output_dir: Path, ffdec_jar: Path) -> None:
	PetAnimExporter(pet_id, output_dir, ffdec_jar).export()


def export_all_pet_animations(
	monsters_json_path: Path,
	plugin_base_dir: Path,
	ffdec_jar_path: Path,
	extra_pet_ids: list[int] | None = None,
) -> None:
	"""遍历精灵列表，导出尚未生成的战斗动画。"""
	if ffdec_jar_path and not ffdec_jar_path.exists():
		print(f"未找到 ffdec.jar：{ffdec_jar_path}，跳过精灵动画导出")
		return

	pet_anim_base = plugin_base_dir / "动画"
	pet_anim_base.mkdir(parents=True, exist_ok=True)

	with open(monsters_json_path, "r", encoding="utf-8") as f:
		monsters = json.load(f)["Monsters"]["Monster"]

	pet_ids = [m["ID"] for m in monsters]
	for pet_id in extra_pet_ids or []:
		if pet_id not in pet_ids:
			pet_ids.append(pet_id)

	for pet_id in pet_ids:
		out_dir = pet_anim_base / str(pet_id)
		if pet_anim_export_complete(out_dir):
			print(f"跳过 {pet_id}：已有导出 ({out_dir})")
			continue
		try:
			print(f"导出动画 {pet_id} ...")
			export_pet_animations(pet_id, out_dir, ffdec_jar_path)
		except Exception as e:
			print(f"❌ {pet_id} 动画导出失败：{e}")
