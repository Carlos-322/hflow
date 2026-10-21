"""Local Realman MCAP preflight. Raw bytes stay authoritative; no VLM calls.

Run with: python -m hflow.realman_qc --input FILE_OR_DIRECTORY --config CONFIG
"""
# Chinese user-facing report strings intentionally use Chinese punctuation.
# ruff: noqa: RUF001

from __future__ import annotations

import argparse
import asyncio
import hashlib
import html
import json
import math
import shutil
import time
from collections import Counter
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import cv2
import numpy as np
from mcap.records import Channel, DataEnd, Footer, Message, Schema
from mcap.stream_reader import StreamReader
from mcap_ros2.decoder import DecoderFactory
from pydantic import BaseModel, ConfigDict, Field, field_validator

import hflow
from hflow.checks import required_topics, timestamp_regularity
from hflow.episode import Episode

VERSION = "realman-qc-v1"
MAGIC = b"\x89MCAP0\r\n"


class StateRule(BaseModel):
    model_config = ConfigDict(extra="forbid")
    field: str = "joint_state.position"
    expected_size: int | None = Field(default=None, gt=0)
    # Units have to be confirmed before speed/acceleration can be gated.
    units: str = "unconfirmed"


class QCConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    profile: str = "Realman preliminary"
    calibrated: bool = False
    required_cameras: list[str] = Field(min_length=1)
    states: dict[str, StateRule]
    expected_hz: dict[str, float] = Field(default_factory=dict)
    gap_factor: float = Field(default=3.0, gt=1)
    sync_tolerance_ms: float = Field(default=50.0, gt=0)
    black_pixel_threshold: int = Field(default=16, ge=0, le=255)
    black_pixel_fraction: float = Field(default=0.98, gt=0, le=1)
    freeze_mean_delta: float = Field(default=0.5, ge=0)
    freeze_min_s: float = Field(default=2.0, gt=0)
    blur_laplacian_threshold: float = Field(default=30.0, gt=0)
    max_examples: int = Field(default=20, gt=0, le=200)

    @field_validator("expected_hz")
    @classmethod
    def positive_rates(cls, rates: dict[str, float]) -> dict[str, float]:
        if any(not math.isfinite(rate) or rate <= 0 for rate in rates.values()):
            raise ValueError("expected_hz must contain finite positive rates")
        return rates

    @field_validator("states")
    @classmethod
    def nonempty_states(cls, states: dict[str, StateRule]) -> dict[str, StateRule]:
        if not states:
            raise ValueError("at least one required robot state topic is needed")
        return states


def sha256(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def nested_value(message: Any, dotted: str) -> Any:
    for name in dotted.split("."):
        message = message[name] if isinstance(message, dict) else getattr(message, name)
    return message


@dataclass
class Topic:
    name: str
    schema: str
    channel_ids: set[int] = field(default_factory=set)
    stamps: list[int] = field(default_factory=list)
    header_stamps: list[int] = field(default_factory=list)
    offsets_ms: list[float] = field(default_factory=list)
    decoded: int = 0
    errors: Counter[str] = field(default_factory=Counter)
    examples: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    dimensions: Counter[str] = field(default_factory=Counter)
    black: int = 0
    blurry: int = 0
    previous_image: Any = None
    previous_image_stamp: int | None = None
    freeze_start: int | None = None
    freezes: list[dict[str, Any]] = field(default_factory=list)
    state_sizes: Counter[int] = field(default_factory=Counter)
    finite_states: int = 0
    previous_state: Any = None
    max_position_step: float = 0.0

    def issue(self, code: str, stamp: int, detail: str, config: QCConfig) -> None:
        self.errors[code] += 1
        examples = self.examples.setdefault(code, [])
        if len(examples) < config.max_examples:
            examples.append({"log_time_ns": stamp, "detail": detail[:500]})

    def end_freeze(self, config: QCConfig) -> None:
        if self.freeze_start is not None and self.previous_image_stamp is not None:
            seconds = (self.previous_image_stamp - self.freeze_start) / 1e9
            if seconds >= config.freeze_min_s:
                self.freezes.append(
                    {
                        "start_ns": self.freeze_start,
                        "end_ns": self.previous_image_stamp,
                        "duration_s": seconds,
                    }
                )
        self.freeze_start = None


def inspect_image(topic: Topic, decoded: Any, stamp: int, config: QCConfig) -> None:
    payload = bytes(decoded.data)
    # Realman chest-camera buffers contain zero padding after JPEG EOI.
    # OpenCV may accept a truncated JPEG, so check EOI independently.
    if payload.startswith(b"\xff\xd8") and not payload.rstrip(b"\x00").endswith(b"\xff\xd9"):
        topic.issue("image_missing_jpeg_end", stamp, "JPEG 缺少结束标记", config)
    image = cv2.imdecode(np.frombuffer(payload, np.uint8), cv2.IMREAD_GRAYSCALE)
    if image is None or image.size == 0:
        topic.issue("image_decode_error", stamp, "图像无法解码", config)
        topic.end_freeze(config)
        topic.previous_image = None
        return
    topic.decoded += 1
    topic.dimensions[f"{image.shape[1]}x{image.shape[0]}"] += 1
    if float(np.mean(image <= config.black_pixel_threshold)) >= config.black_pixel_fraction:
        topic.black += 1
        topic.issue("black_frame", stamp, "画面达到疑似黑帧阈值", config)
    small = cv2.resize(image, (160, 90), interpolation=cv2.INTER_AREA)
    if float(cv2.Laplacian(small, cv2.CV_64F).var()) < config.blur_laplacian_threshold:
        topic.blurry += 1
    delta_s = (
        (stamp - topic.previous_image_stamp) / 1e9
        if topic.previous_image_stamp is not None
        else 0.0
    )
    # Do not turn absent frames or duplicate stamps into a freeze duration.
    maximum_gap = config.gap_factor / config.expected_hz.get(topic.name, 10.0)
    similar = (
        topic.previous_image is not None
        and 0 < delta_s <= maximum_gap
        and float(np.abs(small.astype(np.float32) - topic.previous_image).mean())
        <= config.freeze_mean_delta
    )
    if similar and topic.freeze_start is None:
        topic.freeze_start = topic.previous_image_stamp
    if not similar:
        topic.end_freeze(config)
    topic.previous_image = small.astype(np.float32)
    topic.previous_image_stamp = stamp


def inspect_state(topic: Topic, decoded: Any, stamp: int, config: QCConfig) -> None:
    rule = config.states[topic.name]
    values = np.asarray(nested_value(decoded, rule.field), dtype=float)
    topic.decoded += 1
    topic.state_sizes[int(values.size)] += 1
    if values.ndim != 1 or values.size == 0:
        topic.issue("state_shape", stamp, f"{rule.field}: shape={values.shape}", config)
        return
    if rule.expected_size is not None and values.size != rule.expected_size:
        topic.issue(
            "state_shape", stamp, f"expected={rule.expected_size}, got={values.size}", config
        )
    if not np.isfinite(values).all():
        topic.issue("state_nonfinite", stamp, f"{rule.field} 含 NaN/Inf", config)
        topic.previous_state = None
        return
    topic.finite_states += 1
    if topic.previous_state is not None and values.shape == topic.previous_state.shape:
        topic.max_position_step = max(
            topic.max_position_step, float(np.max(np.abs(values - topic.previous_state)))
        )
    topic.previous_state = values
    # Optional numeric telemetry is checked when populated, not required.
    for name in ("joint_state.velocity", "joint_state.effort", "joint_acc", "gripper_pos"):
        try:
            auxiliary = np.asarray(nested_value(decoded, name), dtype=float)
        except (AttributeError, KeyError):
            continue
        if not np.isfinite(auxiliary).all():
            topic.issue("state_nonfinite", stamp, f"{name} 含 NaN/Inf", config)


def timing(stamps: list[int], rate: float | None, config: QCConfig) -> dict[str, Any]:
    values = np.asarray(stamps, dtype=np.int64)
    if values.size < 2:
        return {"sample_count": len(stamps), "measurable": False}
    deltas = np.diff(values) / 1e9
    positive = deltas[deltas > 0]
    period = 1 / rate if rate else float(np.median(positive)) if positive.size else None
    gap_indices = np.flatnonzero(deltas > period * config.gap_factor) if period else []
    duration = (int(values.max()) - int(values.min())) / 1e9
    return {
        "sample_count": len(stamps),
        "measurable": bool(positive.size),
        "duration_s": duration,
        "mean_hz": (len(stamps) - 1) / duration if duration > 0 else None,
        "median_hz": 1 / float(np.median(positive)) if positive.size else None,
        "expected_hz": rate,
        "threshold_basis": "configured" if rate else "median_positive_period",
        "duplicate_adjacent_count": int(np.sum(deltas == 0)),
        "out_of_order_count": int(np.sum(deltas < 0)),
        "max_gap_s": float(positive.max()) if positive.size else None,
        "gap_count": len(gap_indices),
        "gap_examples": [
            {
                "start_ns": int(values[i]),
                "end_ns": int(values[i + 1]),
                "duration_s": float(deltas[i]),
            }
            for i in list(gap_indices)[: config.max_examples]
        ],
    }


def nearest_sync(camera: Topic, state: Topic, config: QCConfig) -> dict[str, Any]:
    left = np.asarray(camera.stamps, dtype=np.int64)
    right = np.sort(np.asarray(state.stamps, dtype=np.int64))
    index = np.searchsorted(right, left)
    before = right[np.clip(index - 1, 0, len(right) - 1)]
    after = right[np.clip(index, 0, len(right) - 1)]
    distance_ms = np.minimum(np.abs(left - before), np.abs(left - after)) / 1e6
    bad = np.flatnonzero(distance_ms > config.sync_tolerance_ms)
    return {
        "camera": camera.name,
        "state": state.name,
        "clock": "MCAP log_time; nearest sample, not calibrated hardware synchronization",
        "p95_ms": float(np.percentile(distance_ms, 95)),
        "max_ms": float(distance_ms.max()),
        "outside_tolerance_count": len(bad),
        "examples": [
            {"log_time_ns": int(left[i]), "distance_ms": float(distance_ms[i])}
            for i in bad[: config.max_examples]
        ],
    }


def scan_source(path: Path, config: QCConfig) -> tuple[dict[str, Any], dict[str, Topic]]:
    topics: dict[str, Topic] = {}
    channels: dict[int, Channel] = {}
    schemas: dict[int, Schema] = {}
    decoders: dict[int, Any] = {}
    factory = DecoderFactory()
    footer_seen = data_end_seen = False
    error = None
    with path.open("rb") as stream:
        head_ok = stream.read(8) == MAGIC
        stream.seek(max(0, path.stat().st_size - 8))
        tail_ok = stream.read(8) == MAGIC
        stream.seek(0)
        try:
            for record in StreamReader(stream, validate_crcs=True).records:
                if isinstance(record, Schema):
                    schemas[record.id] = record
                elif isinstance(record, Channel):
                    channels[record.id] = record
                elif isinstance(record, Footer):
                    footer_seen = True
                elif isinstance(record, DataEnd):
                    data_end_seen = True
                elif isinstance(record, Message):
                    channel = channels[record.channel_id]
                    schema = schemas.get(channel.schema_id)
                    schema_name = schema.name if schema else ""
                    topic = topics.setdefault(channel.topic, Topic(channel.topic, schema_name))
                    topic.channel_ids.add(channel.id)
                    topic.stamps.append(record.log_time)
                    camera = schema_name == "sensor_msgs/msg/CompressedImage"
                    state = channel.topic in config.states
                    if not camera and not state:
                        continue
                    try:
                        if channel.id not in decoders:
                            decoders[channel.id] = factory.decoder_for(
                                channel.message_encoding, schema
                            )
                        decoder = decoders[channel.id]
                        if decoder is None:
                            raise ValueError(f"unsupported schema/encoding: {schema_name}")
                        decoded = decoder(record.data)
                        if hasattr(decoded, "header"):
                            stamp = decoded.header.stamp
                            ns = int(stamp.sec) * 1_000_000_000 + int(stamp.nanosec)
                            topic.header_stamps.append(ns)
                            topic.offsets_ms.append((record.log_time - ns) / 1e6)
                            if ns <= 0 or not 0 <= int(stamp.nanosec) < 1_000_000_000:
                                topic.issue(
                                    "invalid_header_stamp", record.log_time, str(ns), config
                                )
                        if camera:
                            inspect_image(topic, decoded, record.log_time, config)
                        else:
                            inspect_state(topic, decoded, record.log_time, config)
                    except Exception as exc:
                        topic.issue(
                            "payload_decode_error",
                            record.log_time,
                            f"{type(exc).__name__}: {exc}",
                            config,
                        )
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"[:1500]
        bytes_read = stream.tell()
    for topic in topics.values():
        topic.end_freeze(config)
    return {
        "head_magic_ok": head_ok,
        "tail_magic_ok": tail_ok,
        "data_end_seen": data_end_seen,
        "footer_seen": footer_seen,
        "complete": head_ok and tail_ok and footer_seen and data_end_seen and error is None,
        "scan_error": error,
        "bytes_read": bytes_read,
        "crc_policy": "MCAP library validates nonzero chunk and data-section CRCs; zero CRC is not evidence of a checksum",
        "message_count": sum(len(topic.stamps) for topic in topics.values()),
        "coverage": "full" if footer_seen and error is None else "readable_prefix_only",
    }, topics


async def hflow_checks(path: Path, config: QCConfig) -> dict[str, Any]:
    with Episode(path) as episode:
        required = await required_topics(episode, topics=[*config.required_cameras, *config.states])
        timestamps = await timestamp_regularity(
            episode, expected_hz=config.expected_hz, gap_factor=config.gap_factor
        )
        return {
            "status": "completed",
            "engine_version": hflow.__version__,
            "required_topics": asdict(required),
            "timestamp_regularity": asdict(timestamps),
        }


def inspect_file(path: Path, config: QCConfig) -> dict[str, Any]:
    started = time.monotonic()
    source_hash = sha256(path)
    integrity, topics = scan_source(path, config)
    findings: list[dict[str, str]] = []

    def finding(severity: str, code: str, topic: str, description: str) -> None:
        findings.append(
            {"severity": severity, "code": code, "topic": topic, "description": description}
        )

    if not integrity["complete"]:
        finding(
            "FAIL",
            "incomplete_mcap",
            "",
            "文件结构或 CRC 检查失败，只能报告成功读出的数据，不能验收通过。",
        )
    required = set(config.required_cameras) | set(config.states)
    for name in sorted(required - topics.keys()):
        finding("FAIL", "missing_required_topic", name, "缺少当前配置要求的相机或机器人状态。")
    results: dict[str, Any] = {}
    for name, topic in topics.items():
        camera = topic.schema == "sensor_msgs/msg/CompressedImage"
        clock = timing(topic.stamps, config.expected_hz.get(name), config)
        if len(topic.channel_ids) > 1:
            finding(
                "REVIEW",
                "multiple_channels",
                name,
                "同一 topic 存在多个 channel；本报告合并计数，需核查来源。",
            )
        if clock.get("duplicate_adjacent_count", 0) or clock.get("out_of_order_count", 0):
            finding(
                "REVIEW", "timestamp_order", name, "记录时间有相邻重复或倒退，详见 log_timing。"
            )
        if clock.get("gap_count", 0) and (
            camera or name in config.states or name in config.expected_hz
        ):
            finding(
                "REVIEW",
                "timestamp_gaps",
                name,
                f"发现 {clock['gap_count']} 处超过阈值的记录间隔。",
            )
        if name in required and not clock["measurable"]:
            finding("FAIL", "insufficient_timestamps", name, "不足两个有效时间点，无法检查连续性。")
        expected = config.expected_hz.get(name)
        observed = clock.get("mean_hz")
        if expected and observed is not None and abs(observed - expected) / expected > 0.2:
            finding(
                "REVIEW",
                "rate_mismatch",
                name,
                f"平均频率 {observed:.2f} Hz，暂定期望 {expected:g} Hz。",
            )
        if name in config.required_cameras and not camera:
            finding(
                "FAIL",
                "unsupported_camera",
                name,
                f"当前适配器不能完整检查此相机格式：{topic.schema}",
            )
        if topic.errors:
            hard = set(topic.errors) - {"black_frame", "invalid_header_stamp"}
            if hard:
                finding(
                    "FAIL",
                    "payload_invalid",
                    name,
                    f"解码、图像结构或状态数值异常：{', '.join(sorted(hard))}",
                )
            if topic.errors.get("invalid_header_stamp"):
                finding("REVIEW", "invalid_header_stamp", name, "存在零值或非法设备时间戳。")
        header_clock = timing(topic.header_stamps, None, config)
        if header_clock.get("duplicate_adjacent_count", 0) or header_clock.get(
            "out_of_order_count", 0
        ):
            finding(
                "REVIEW", "header_timestamp_order", name, "设备 header 时间戳有相邻重复或倒退。"
            )
        if camera and topic.decoded == 0:
            finding("FAIL", "no_decoded_images", name, "没有一帧可读取的图像。")
        if camera and topic.black:
            finding("REVIEW", "black_frames", name, f"{topic.black}/{topic.decoded} 帧疑似黑帧。")
        if camera and topic.freezes:
            finding(
                "REVIEW",
                "near_static",
                name,
                f"{len(topic.freezes)} 段画面变化很小；可能是正常停顿。",
            )
        if camera and topic.blurry / max(topic.decoded, 1) > 0.5:
            finding(
                "REVIEW",
                "low_texture",
                name,
                "超过半数画面清晰度指标偏低；可能是低纹理场景，需看图确认。",
            )
        if len(topic.dimensions) > 1:
            finding("REVIEW", "resolution_changes", name, "相机分辨率中途变化。")
        if len(topic.state_sizes) > 1:
            finding("FAIL", "state_dimension_changes", name, "机器人状态向量长度中途变化。")
        results[name] = {
            "schema": topic.schema,
            "channel_ids": sorted(topic.channel_ids),
            "message_count": len(topic.stamps),
            "payload_check": "all_read_messages"
            if camera or name in config.states
            else "not_in_profile",
            "decoded_count": topic.decoded,
            "log_timing": clock,
            "header_timing": header_clock,
            "header_offset_ms": {
                "median": float(np.median(topic.offsets_ms)),
                "min": min(topic.offsets_ms),
                "max": max(topic.offsets_ms),
            }
            if topic.offsets_ms
            else None,
            "error_counts": dict(topic.errors),
            "error_examples": topic.examples,
            "image_dimensions": dict(topic.dimensions),
            "black_frames": topic.black,
            "low_texture_frames": topic.blurry,
            "near_static_intervals": topic.freezes,
            "state_sizes": dict(topic.state_sizes),
            "finite_state_count": topic.finite_states,
            "max_position_step_in_source_units": topic.max_position_step
            if topic.state_sizes
            else None,
        }
    sync = []
    for camera in config.required_cameras:
        for state in config.states:
            if camera in topics and state in topics:
                measurement = nearest_sync(topics[camera], topics[state], config)
                sync.append(measurement)
                if measurement["outside_tolerance_count"]:
                    finding(
                        "REVIEW",
                        "camera_state_sync",
                        camera,
                        f"与 {state} 存在超过 {config.sync_tolerance_ms:g} ms 的最近采样时间差。",
                    )
    if integrity["complete"]:
        try:
            native = asyncio.run(hflow_checks(path, config))
        except Exception as exc:
            native = {"status": "error", "error": f"{type(exc).__name__}: {exc}"[:1500]}
            finding("ERROR", "hflow_check_failed", "", "HFlow 内置检查未完成，不能自动放行。")
    else:
        native = {
            "status": "skipped",
            "reason": "原始 MCAP 不完整；保留原始结构缺陷，不自动修复后放行。",
        }
    source_unchanged = sha256(path) == source_hash
    if not source_unchanged:
        finding("ERROR", "source_changed", "", "检查前后文件哈希不一致，本次结果不可使用。")
    if not config.calibrated:
        finding(
            "REVIEW",
            "uncalibrated_profile",
            "",
            "当前为初步阈值配置，尚未按公司验收标准校准，不提供最终业务验收结论。",
        )
    status = next(
        (
            level
            for level in ("ERROR", "FAIL", "REVIEW")
            if any(f["severity"] == level for f in findings)
        ),
        "PASS",
    )
    all_first = [min(topic.stamps) for topic in topics.values()]
    all_last = [max(topic.stamps) for topic in topics.values()]
    return {
        "adapter_version": VERSION,
        "file": path.name,
        "source_path": str(path),
        "sha256": source_hash,
        "source_unchanged": source_unchanged,
        "size_bytes": path.stat().st_size,
        "quality_status": status,
        "execution_status": "error" if status == "ERROR" else "completed",
        "elapsed_s": round(time.monotonic() - started, 2),
        "time_origin_ns": min(all_first) if all_first else None,
        "readable_duration_s": (max(all_last) - min(all_first)) / 1e9 if all_first else 0,
        "integrity": integrity,
        "findings": findings,
        "topics": results,
        "camera_state_sync": sync,
        "hflow_checks": native,
        "limitations": [
            "只做确定性技术质检，不判断任务成功、动作正确或子任务划分。",
            "仅解码所有 ROS2 CompressedImage 和配置指定的状态；其他 topic 只查记录时间和数量。",
            "相机在原始 JPEG/PNG 上逐帧解码；黑帧用原尺寸灰度，静止/清晰度用 160x90 灰度。",
            "静止与清晰度指标只是复核线索。未检查视野遮挡、目标可见性、标定精度或物理动作安全性。",
            "最近采样时间差基于 log_time，不证明传感器硬件同步。状态单位未确认，不判定速度/加速度超限。",
            "损坏 chunk 中无法解析的消息不计入已检查数量。缺少尾部时无法推断实际丢失多少数据。",
            "HFlow 内置时间检查读取同一原始 MCAP；未调用需要转码的 camera_frame_stats。",
        ],
    }


def render_html(batch: dict[str, Any]) -> str:
    def escape(value: Any) -> str:
        return html.escape(str(value))

    cards = []
    labels = {
        "PASS": "技术检查通过",
        "REVIEW": "需要复核",
        "FAIL": "发现明确问题",
        "ERROR": "检查未完成",
    }
    for result in batch["results"]:
        status = result["quality_status"]
        origin = result.get("time_origin_ns") or 0
        evidence_rows = []
        for name, data in result.get("topics", {}).items():
            if data["payload_check"] != "all_read_messages":
                continue
            for interval in data["log_timing"].get("gap_examples", [])[:5]:
                start = (interval["start_ns"] - origin) / 1e9
                end = (interval["end_ns"] - origin) / 1e9
                evidence_rows.append(
                    f"<tr><td>{escape(name)}</td><td>记录间隔偏长</td><td>{start:.3f}–{end:.3f} 秒</td></tr>"
                )
            for interval in data["near_static_intervals"][:5]:
                start = (interval["start_ns"] - origin) / 1e9
                end = (interval["end_ns"] - origin) / 1e9
                evidence_rows.append(
                    f"<tr><td>{escape(name)}</td><td>画面变化很小</td><td>{start:.3f}–{end:.3f} 秒</td></tr>"
                )
            for code, examples in data["error_examples"].items():
                for example in examples[:5]:
                    seconds = (example["log_time_ns"] - origin) / 1e9
                    evidence_rows.append(
                        f"<tr><td>{escape(name)}</td><td>{escape(code)}</td><td>{seconds:.3f} 秒</td></tr>"
                    )
        rows = "".join(
            f"<tr><td>{escape(item['severity'])}</td><td>{escape(item['topic'])}</td><td>{escape(item['description'])}</td></tr>"
            for item in result.get("findings", [])
        )
        topic_rows = "".join(
            f"<tr><td>{escape(name)}</td><td>{data['message_count']}</td><td>{data['decoded_count']}</td>"
            f"<td>{escape(data['log_timing'].get('mean_hz'))}</td><td>{escape(data['error_counts'])}</td></tr>"
            for name, data in result.get("topics", {}).items()
        )
        cards.append(
            f"<section><h2>{escape(result['file'])}</h2><p class='{status}'>{labels[status]}</p>"
            f"<p>耗时：{result.get('elapsed_s', 0)} 秒；可读时长：{result.get('readable_duration_s', 0):.2f} 秒；"
            f"已读消息：{result.get('integrity', {}).get('message_count', 0)}；"
            f"覆盖范围：{escape(result.get('integrity', {}).get('coverage', 'none'))}</p>"
            f"<p>输入未改变：{escape(result.get('source_unchanged', '未知'))}；SHA256：<code>{escape(result.get('sha256', ''))}</code></p>"
            f"<table><tr><th>级别</th><th>数据流</th><th>问题</th></tr>{rows}</table>"
            f"<details><summary>问题定位：片段内秒数（每类每路最多展示 5 个例子）</summary>"
            f"<table><tr><th>数据流</th><th>现象</th><th>相对片段开始的时间</th></tr>{''.join(evidence_rows)}</table></details>"
            f"<details><summary>展开各数据流统计（decoded=0 可能表示不在内容检查范围）</summary>"
            f"<table><tr><th>Topic</th><th>消息数</th><th>已解码</th><th>平均 Hz</th><th>异常计数</th></tr>{topic_rows}</table></details>"
            f"<details><summary>展开完整证据，包括异常时间点</summary><pre>{escape(json.dumps(result, ensure_ascii=False, indent=2))}</pre></details></section>"
        )
    return (
        "<!doctype html><html lang='zh-CN'><meta charset='utf-8'><title>HFlow 本地质检报告</title>"
        "<style>body{font:16px system-ui;margin:32px auto;max-width:1300px;padding:0 24px;color:#203535;background:#f4f7f7}"
        "section{background:white;padding:24px;margin:24px 0;border-radius:12px}table{border-collapse:collapse;width:100%;font-size:14px}"
        "td,th{padding:10px;border:1px solid #ddd;text-align:left;overflow-wrap:anywhere}h2,code{overflow-wrap:anywhere}"
        ".FAIL,.ERROR{color:#b42318;font-weight:bold}.REVIEW{color:#986400;font-weight:bold}.PASS{color:#137454}"
        "pre{white-space:pre-wrap;overflow-wrap:anywhere;font-size:12px}details{margin:20px 0}summary{cursor:pointer}</style>"
        f"<h1>HFlow · Realman 数据质检</h1><p>{escape(batch['created_at'])}</p>"
        "<p>读取副本 → 文件完整性 → 逐帧图像与状态检查 → 时间连续性与同步 → 问题报告</p>"
        "<p>这是技术预检。需复核不等于采集失败；不完整文件不能验收通过。当前不包含任务成功判断或 AI 语义审核。</p>"
        "<p>异常时间点使用 MCAP log_time 纳秒；减去 time_origin_ns 再除以 10⁹ 可换算成片段内秒数。"
        "相邻重复和倒退按文件记录顺序统计；跨帧缺口保留起止时间。完整数据见同目录 report.json。</p>"
        + "".join(cards)
        + "</html>"
    )


def run_batch(
    input_path: Path, config_path: Path, runtime_root: Path, export_root: Path
) -> tuple[Path, int]:
    config_bytes = config_path.read_bytes()
    config = QCConfig.model_validate_json(config_bytes.decode("utf-8-sig"))
    sources = sorted(input_path.glob("*.mcap")) if input_path.is_dir() else [input_path]
    if not sources or any(not path.is_file() or path.suffix.lower() != ".mcap" for path in sources):
        raise ValueError("输入位置没有 MCAP 文件。目录模式只读取当前目录的 *.mcap。")
    run_id = datetime.now(UTC).strftime("%Y%m%dT%H%M%S-%fZ")
    output = export_root / run_id
    output.mkdir(parents=True, exist_ok=False)
    cache = runtime_root / "inputs"
    cache.mkdir(parents=True, exist_ok=True)
    results = []
    for source in sources:
        print(f"Inspecting: {source.name}", flush=True)
        try:
            digest = sha256(source)
            copied = cache / digest / source.name
            copied.parent.mkdir(parents=True, exist_ok=True)
            if not copied.exists() or sha256(copied) != digest:
                shutil.copyfile(source, copied)
            if sha256(copied) != digest:
                raise RuntimeError("输入复制后哈希验证失败")
            result = inspect_file(copied, config)
            result["original_path"] = str(source)
            result["original_unchanged"] = sha256(source) == digest
            if not result["original_unchanged"] or result["sha256"] != digest:
                result["quality_status"] = "ERROR"
                result["execution_status"] = "error"
                result["findings"].append(
                    {
                        "severity": "ERROR",
                        "code": "input_changed",
                        "topic": "",
                        "description": "复制或检查期间输入改变，本次结果不可使用。",
                    }
                )
        except Exception as exc:
            result = {
                "file": source.name,
                "quality_status": "ERROR",
                "execution_status": "error",
                "findings": [
                    {
                        "severity": "ERROR",
                        "code": "execution_error",
                        "topic": "",
                        "description": f"{type(exc).__name__}: {exc}"[:1500],
                    }
                ],
            }
        results.append(result)
        print(f"  {result['quality_status']}: {result.get('elapsed_s', 0)} s", flush=True)
    batch = {
        "created_at": datetime.now(UTC).isoformat(),
        "adapter_version": VERSION,
        "config": config.model_dump(),
        "config_sha256": hashlib.sha256(config_bytes).hexdigest(),
        "adapter_source_sha256": sha256(Path(__file__)),
        "versions": {
            "hflow": hflow.__version__,
            "opencv": cv2.__version__,
            "numpy": np.__version__,
        },
        "results": results,
    }
    (output / "report.json").write_text(
        json.dumps(batch, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8"
    )
    (output / "report.html").write_text(render_html(batch), encoding="utf-8")
    (output / "config.json").write_text(config.model_dump_json(indent=2), encoding="utf-8")
    # A report pointer is published only after both report formats are complete.
    (export_root / "latest-report.txt").write_text(str(output / "report.html"), encoding="utf-8")
    print(f"Report: {output / 'report.html'}", flush=True)
    code = (
        3
        if any(r["quality_status"] == "ERROR" for r in results)
        else 2
        if any(r["quality_status"] != "PASS" for r in results)
        else 0
    )
    return output, code


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Local deterministic Realman MCAP quality inspection"
    )
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--runtime-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        _, code = run_batch(args.input, args.config, args.runtime_root, args.output)
        return code
    except Exception as exc:
        print(f"QC startup failed: {type(exc).__name__}: {exc}", flush=True)
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
