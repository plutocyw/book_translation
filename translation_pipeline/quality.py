"""Deterministic release gate and artifact invalidation helpers.

The translation commands deliberately remain usable while work is in progress.
This module is the stricter boundary used before a book is declared complete or
published: every selected artifact must be current, reviewed, internally
consistent, and represented exactly in the assembled book.
"""

from __future__ import annotations

import csv
import hashlib
import json
import re
from collections import Counter
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Set, Union


JsonMap = Mapping[str, Any]
Manifest = Optional[Union[Path, str, JsonMap]]

STAGES: Sequence[str] = (
    "metadata",
    "extract",
    "chunk",
    "terms",
    "draft",
    "review",
    "finalize",
    "qa",
    "assemble",
    "notion",
)

# Earliest stage affected by a dependency change.  Everything downstream is
# stale as well.  Config is conservatively rooted at metadata because it may
# alter source boundaries, languages, chunk sizing, or model routing.
DEPENDENCY_STAGE: Mapping[str, str] = {
    "source": "extract",
    "config": "metadata",
    "prompts": "terms",
    "prompt": "terms",
    "references": "terms",
    "reference": "terms",
    "models": "terms",
    "model": "terms",
}

# Simplified-only forms with a high signal in Taiwan Traditional Chinese.  We
# intentionally omit ambiguous characters such as 后, 里, 干, 面, 台, 只 and 于.
HIGH_CONFIDENCE_SIMPLIFIED = frozenset(
    "这们为与业东丝丢两严丧丰临丽举义乌乐乔习乡书买乱争亏云亚产亩亲亵亿仅从"
    "仓仪价众优会伛伞伟传伤伦伪体佣侠侣侥侦侧侨侩俩俭债倾偿党兰关兴养兽冈"
    "册写军农冯冲决况冻净凉减凑凤凭凯击凿刘则刚创删刹剂剑剧劝办务动励劲"
    "劳势勋区医华协单卖卢卫厂厅历厉压厌厕县叁参双变叙叠叶号叹吗吕听启吴呐"
    "员呛呜咏咙响哑哒哓哔哗哙哟唤啧喷喽噜团园围国图圆圣场坏块坚坛坝坟坠"
    "垄垒垦执扩扫扬扰抚抛抢护报担拟拢拣拥拦拧拨择挂挚挛挝挞挟挠挡挣挤挥"
    "捞损捡换据掳掸掺揽搀搁搂搅携摄摆摇摊撑撵敌敛数斋斓斩断无旧时旷昙显"
    "晋晒晓晕暂术朴机杀杂权条来杨极构枪柜标栈栋栏树样桥桨梦检楼榄欢欧歼残"
    "毁毕毙气汇汉汤沟没沣沤沥沦沧沨沩沪泪泼泽洁洒浅浆浇测济浑浓涂涌涛涝"
    "涟涡涣涤润涧涨涩淀渊渍渐渔渗温湾湿溃溅滚滞满滤滥滨滩潆潇潋潜澜濒灭"
    "灯灵灾灿炉点炼烁烂烛烟烦烧烨烫热焕爱爷牍牵犊状犹狈狞独狭狮狱猎猪猫"
    "献獭玛环现玱玺珑琐琼电画畅畴疗疟疡疬疮疯痪痫瘅瘆瘾瘿皑皱盏盐监盖盘眍"
    "着睁瞒瞩矫矿码砖砚砺础硕确碍碛礼祷祸禀禄禅离秃秆种积称秽稳窜窝窍窦竞"
    "笔笋笼筑筛筹签简箓箩箫篓篮篱类籴粤粪粮紧纠红纤约级纪纬纯纱纲纳纵纷纸"
    "纹纺纽线练组绅细织终绊绍绎经绑绒结绕绘给绚络绝绞统绣继绩绪续绳维绵绷"
    "绸综绿缀缄缅缆缉缎缓缔缕编缘缚缝缠缨缩缴罢罗罚罴羁翘耸耻聂聋职联聪肃"
    "肠肤肾肿胀胁胆胜胶脉脏脐脑脓脚脱脸腊腻腼腾膑臜舆舰舱艳艺节芜苁苇苍苏"
    "苹范茎茧荆荐荡荣荤荧药莱莲获莹莺萝萤营萦萧萨葱蒋蓝蓟蔷蔺蕲蕴薮藓虏虑"
    "虚虫虽虾蚀蚁蚂蛊蛮蛰蛱蝇蝉蝎蝼衅衔补衬袄袭袜装裤裢裣裤见观规觅视览觉"
    "觊觋觌觎觏觐觑触誉誊讣认讥讦讧讨让讪训议讯记讲讳讴讶讷许讹论讼讽设访"
    "诀证评诅识诈诉诊词译试诗诚诛话诞诡询诣该详诫诬语误诰诱诲说诵请诸诺读"
    "诽课谁调谅谈谊谋谍谎谏谐谓谕谗谘谙谛谜谢谣谤谦谨谩谱谴谷贝贞负贡财责贤"
    "败账货质贩贪贫贬购贮贯贰贱贲贴贵贷贸费贺贼贾贿赀赁赂赃资赈赉赋赌赎赏"
    "赐赔赖赘赚赛赞赠赡赢赵赶趋跃跻践踊踪踬踯蹑蹒蹿躏躯车轨轩转轮软轰轴轻"
    "载轿较辅辆辈辉辐辑输辕辖辙辞辩辫边辽达迁过迈运还进远违连迟迩迹适选逊"
    "递逻遗遥邓邝郑邻郁郏郐郓郦邮酝酱酿释鉴钟钠钢钥钦钧钩钮钱钳钻铁铃铅铆"
    "铜铝铠铲银铸铺链销锁锄锅锈锋锚锡锣锤锦锯锹锻镀镇镐镑镜镣镰镶长门闪闭"
    "问闯闰闲间闷闸闹闺闻闽阀阁阅阉阎阐阔队阳阴阵阶际陆陈陕险随隐隶难雏雳"
    "雾霁静鞑鞯韦韧韩页顶顷项顺须顽顾顿颁颂预颅领颇颈频颓颖颗题颜额颠颤风"
    "飒飘飞饥饭饮饯饰饱饲饵饶饺饿馁馆馈馋馒马驭驮驯驰驱驳驴驶驷驸驻驼驾骂"
    "骄骆骇验骏骑骗骚骤鱼鲁鲍鲜鲤鲸鸟鸡鸣鸥鸦鸭鸯鸳鸵鸽鸿鹃鹅鹊鹏鹤鹰麦黄"
    "黉齐齿龄龙龟"
) - frozenset("云余佣冲据朴杆着谷范涂郁丰")

PLACEHOLDER_RE = re.compile(
    r"(?i)(?:\b(?:TODO|TBD|FIXME|LOREM\s+IPSUM|TRANSLATION)\b|\?\?\?|\[\[.+?\]\]|\{\{.+?\}\}|<placeholder>)"
)
LATIN_RUN_RE = re.compile(r"[A-Za-z][A-Za-z'’-]*(?:[ \t]+[A-Za-z][A-Za-z'’-]*){4,}")
COMMON_ENGLISH = frozenset(
    "a an and are as at be been but by for from had has have he her him his i in is it its not of on or she that the their them they this to was were will with you your".split()
)
_NUMBER_WORD = (
    r"(?:One|Two|Three|Four|Five|Six|Seven|Eight|Nine|Ten|Eleven|Twelve|Thirteen|"
    r"Fourteen|Fifteen|Sixteen|Seventeen|Eighteen|Nineteen|Twenty|Thirty|Forty|"
    r"Fifty|Sixty|Seventy|Eighty|Ninety)(?:[- ](?:One|Two|Three|Four|Five|Six|"
    r"Seven|Eight|Nine))?"
)
SOURCE_CHAPTER_RE = re.compile(
    rf"^(?:Prologue|Epilogue|Introduction|Afterword|{_NUMBER_WORD}|"
    rf"(?:Chapter|Part|Book)\s+(?:\d+|[IVXLCDM]+|{_NUMBER_WORD}))$",
    re.IGNORECASE,
)
TARGET_HEADING_RE = re.compile(r"^#{1,6}\s+\S.*$")
MARKER_RE = re.compile(r"^<!--\s*(chunk-\d{4});\s*source pages\s+(\d+)-(\d+)\s*-->$", re.MULTILINE)


@dataclass(frozen=True)
class QualityIssue:
    code: str
    message: str
    chunk_id: Optional[str] = None
    path: Optional[str] = None
    details: Dict[str, Any] = field(default_factory=dict)
    severity: str = "error"


@dataclass
class QualityReport:
    checked_chunks: int
    issues: List[QualityIssue] = field(default_factory=list)
    checks: Dict[str, int] = field(default_factory=dict)

    @property
    def passed(self) -> bool:
        return not any(issue.severity == "error" for issue in self.issues)

    @property
    def error_count(self) -> int:
        return sum(issue.severity == "error" for issue in self.issues)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "passed": self.passed,
            "checked_chunks": self.checked_chunks,
            "error_count": self.error_count,
            "issue_count": len(self.issues),
            "checks": dict(self.checks),
            "issues": [asdict(issue) for issue in self.issues],
        }

    def raise_for_errors(self) -> None:
        if not self.passed:
            raise QualityGateError(self)


class QualityGateError(RuntimeError):
    """Raised when the formal completion gate finds one or more errors."""

    def __init__(self, report: QualityReport):
        self.report = report
        codes = Counter(issue.code for issue in report.issues if issue.severity == "error")
        summary = ", ".join(f"{code}={count}" for code, count in sorted(codes.items()))
        super().__init__(f"Quality gate failed with {report.error_count} error(s): {summary}")


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def pipeline_sha256_text(text: str) -> str:
    """Hash one text value using the pipeline's NUL-delimited convention."""

    return hashlib.sha256(text.encode("utf-8") + b"\0").hexdigest()


def content_hashes(text: str) -> Set[str]:
    """Return accepted exact-content hash encodings.

    Early hand-reviewed books used the conventional raw SHA-256 while automated
    pipeline artifacts use its NUL-delimited multi-part hash helper.  Both bind
    the entire exact text and are therefore safe to verify during migration.
    """

    return {sha256_text(text), pipeline_sha256_text(text)}


def artifact_input_hash(**inputs: Any) -> str:
    """Return a stable provenance hash for a model or deterministic artifact."""

    payload = json.dumps(inputs, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return sha256_text(payload)


def changed_dependencies(previous: JsonMap, current: JsonMap) -> Set[str]:
    """Return recognized dependency groups whose fingerprints changed."""

    return {key for key in DEPENDENCY_STAGE if previous.get(key) != current.get(key)}


def stale_stages_for_changes(changes: Iterable[str]) -> List[str]:
    """Expand dependency changes through the ordered artifact stage DAG."""

    indexes = [STAGES.index(DEPENDENCY_STAGE[key]) for key in set(changes) if key in DEPENDENCY_STAGE]
    return list(STAGES[min(indexes) :]) if indexes else []


def invalidation_plan(previous: JsonMap, current: JsonMap) -> Dict[str, Any]:
    """Describe, without mutating files, which stages must be marked stale."""

    changed = sorted(changed_dependencies(previous, current))
    return {"changed_dependencies": changed, "stale_stages": stale_stages_for_changes(changed)}


def mark_stale(artifacts: JsonMap, stale_stages: Iterable[str]) -> Dict[str, Any]:
    """Return a copy of an artifact-state mapping with affected stages stale."""

    stale = set(stale_stages)
    result: Dict[str, Any] = {}
    for stage, value in artifacts.items():
        if stage not in stale:
            result[stage] = value
        elif isinstance(value, Mapping):
            result[stage] = {**value, "status": "stale"}
        else:
            result[stage] = {"status": "stale", "previous": value}
    return result


def run_quality_gate(
    root: Union[Path, str],
    manifest: Manifest = None,
    *,
    raise_on_error: bool = False,
) -> QualityReport:
    """Validate all chunks and the assembled book under *root*.

    ``manifest`` is intentionally a loose mapping so an orchestrator can record
    justified exceptions without importing one of its classes.  Options may be
    top-level or nested under ``quality``.  Supported keys include:

    * ``paragraph_count_exceptions``: chunk ids mapped to ``true``, an expected
      target count, or ``{"source": n, "target": n, "reason": ...}``.
    * ``simplified_allowlist``, ``english_allowlist``, ``placeholder_allowlist``.
    * ``glossary_exceptions``: chunk ids mapped to source-term lists.
    * ``chosen_translations`` and ``review_input_hashes``.
    """

    root_path = Path(root).resolve()
    manifest_data = _load_manifest(manifest)
    quality = manifest_data.get("quality", manifest_data)
    rows, load_issues = _load_chunks(root_path / "build" / "chunks.jsonl")
    report = QualityReport(checked_chunks=len(rows), issues=load_issues)
    expected_pieces: List[str] = []

    glossary, glossary_issues = _load_glossary(root_path / "context" / "glossary.csv")
    report.issues.extend(glossary_issues)
    generated_path = root_path / "context" / "approved_terminology.json"
    if generated_path.exists():
        try:
            generated = json.loads(generated_path.read_text(encoding="utf-8"))
            glossary.extend(
                {
                    "source_term": str(item.get("source_term", "")),
                    "target_term": str(item.get("target_term", "")),
                    "status": "approved",
                    "category": str(item.get("category", "")),
                    "generated": "true",
                }
                for item in generated.get("terms", [])
            )
            glossary.extend(
                {
                    "source_term": str(item.get("source_name", "")),
                    "target_term": str(item.get("target_name", "")),
                    "status": "approved",
                    "category": "character",
                    "generated": "true",
                }
                for item in generated.get("characters", [])
            )
        except (json.JSONDecodeError, OSError, AttributeError) as exc:
            report.issues.append(QualityIssue("generated_glossary_invalid", str(exc), path=str(generated_path)))

    for row in rows:
        _check_chunk(root_path, row, quality, glossary, report, expected_pieces)

    _check_assembly(root_path, rows, expected_pieces, report)
    report.checks = dict(Counter(issue.code for issue in report.issues))
    report.checks["chunks_checked"] = len(rows)
    report.checks["approved_glossary_entries"] = len(glossary)
    if raise_on_error:
        report.raise_for_errors()
    return report


def assert_quality_gate(root: Union[Path, str], manifest: Manifest = None) -> QualityReport:
    """Run the gate and raise :class:`QualityGateError` on any error."""

    return run_quality_gate(root, manifest, raise_on_error=True)


def _load_manifest(manifest: Manifest) -> Dict[str, Any]:
    if manifest is None:
        return {}
    if isinstance(manifest, Mapping):
        return dict(manifest)
    return json.loads(Path(manifest).read_text(encoding="utf-8"))


def _load_chunks(path: Path) -> tuple[List[Dict[str, Any]], List[QualityIssue]]:
    if not path.exists():
        return [], [QualityIssue("chunks_missing", "build/chunks.jsonl does not exist", path=str(path))]
    rows: List[Dict[str, Any]] = []
    issues: List[QualityIssue] = []
    for number, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not raw.strip():
            continue
        try:
            row = json.loads(raw)
        except json.JSONDecodeError as exc:
            issues.append(QualityIssue("chunk_json_invalid", str(exc), path=str(path), details={"line": number}))
            continue
        rows.append(row)
    expected = [f"chunk-{index:04d}" for index in range(1, len(rows) + 1)]
    actual = [row.get("chunk_id") for row in rows]
    if actual != expected:
        issues.append(
            QualityIssue(
                "chunk_order_invalid",
                "Chunk ids must be unique, contiguous, and in source order",
                path=str(path),
                details={"expected": expected, "actual": actual},
            )
        )
    return rows, issues


def _load_glossary(path: Path) -> tuple[List[Dict[str, str]], List[QualityIssue]]:
    if not path.exists():
        return [], [QualityIssue("glossary_missing", "Approved glossary does not exist", path=str(path))]
    with path.open(encoding="utf-8", newline="") as handle:
        approved = [dict(row) for row in csv.DictReader(handle) if row.get("status") == "approved"]
    targets: Dict[str, Set[str]] = {}
    for row in approved:
        if row.get("source_term") and row.get("target_term"):
            targets.setdefault(row["source_term"].casefold(), set()).add(row["target_term"])
    conflicts = {source: sorted(values) for source, values in targets.items() if len(values) > 1}
    issues = []
    if conflicts:
        issues.append(
            QualityIssue(
                "glossary_conflict",
                "Approved source terms have conflicting target forms",
                path=str(path),
                details={"conflicts": conflicts},
            )
        )
    return approved, issues


def _chosen_path(root: Path, chunk_id: str, quality: JsonMap) -> Path:
    configured = quality.get("chosen_translations", {}).get(chunk_id)
    if configured:
        path = Path(str(configured))
        return path if path.is_absolute() else root / path
    final = root / "output" / "chunks" / f"{chunk_id}.final.zh-Hant.md"
    reviewed = root / "output" / "chunks" / f"{chunk_id}.reviewed.zh-Hant.md"
    normal = root / "output" / "chunks" / f"{chunk_id}.zh-Hant.md"
    return final if final.exists() else reviewed if reviewed.exists() else normal


def _check_chunk(
    root: Path,
    row: Dict[str, Any],
    quality: JsonMap,
    glossary: List[Dict[str, str]],
    report: QualityReport,
    expected_pieces: List[str],
) -> None:
    chunk_id = str(row.get("chunk_id", ""))
    source = str(row.get("source", ""))
    recorded_source_hash = row.get("source_sha256")
    if recorded_source_hash not in content_hashes(source):
        _add(report, "build_source_hash_mismatch", "Source text does not match its build hash", chunk_id,
             details={"recorded": recorded_source_hash, "valid_hashes": sorted(content_hashes(source))})

    target_path = _chosen_path(root, chunk_id, quality)
    if not target_path.exists():
        _add(report, "translation_missing", "Chosen translation does not exist", chunk_id, target_path)
        return
    target = target_path.read_text(encoding="utf-8")
    target_hashes = content_hashes(target)
    reviewed = target_path.name.endswith(".reviewed.zh-Hant.md")
    finalized = target_path.name.endswith(".final.zh-Hant.md")

    meta_path = root / "output" / "chunks" / f"{chunk_id}.meta.json"
    if finalized:
        meta_path = root / "output" / "chunks" / f"{chunk_id}.final.meta.json"
    elif reviewed:
        configured_meta = quality.get("reviewed_meta", {}).get(chunk_id)
        meta_path = (
            (Path(str(configured_meta)) if Path(str(configured_meta)).is_absolute() else root / str(configured_meta))
            if configured_meta
            else root / "output" / "chunks" / f"{chunk_id}.reviewed.meta.json"
        )
    meta = _read_json_artifact(meta_path, "translation_meta", chunk_id, report)
    review_path = root / "output" / "reviews" / f"{chunk_id}.review.json"
    review = _read_json_artifact(review_path, "review", chunk_id, report)

    if meta is not None:
        _hash_field(report, meta, "source_sha256", recorded_source_hash, "meta_source_hash_mismatch", chunk_id, meta_path)
        _hash_field_in(report, meta, "translation_sha256", target_hashes, "meta_translation_hash_mismatch", chunk_id, meta_path)
        if meta.get("chunk_id") != chunk_id:
            _add(report, "meta_chunk_mismatch", "Translation metadata names a different chunk", chunk_id, meta_path,
                 {"actual": meta.get("chunk_id")})
    if review is not None:
        _hash_field(report, review, "source_sha256", recorded_source_hash, "review_source_hash_mismatch", chunk_id, review_path)
        expected_review_hashes = target_hashes
        if finalized:
            base_path = root / "output" / "chunks" / f"{chunk_id}.zh-Hant.md"
            if not base_path.exists():
                _add(report, "finalized_input_missing", "Finalized artifact's draft translation is missing", chunk_id, base_path)
                expected_review_hashes = set()
            else:
                expected_review_hashes = content_hashes(base_path.read_text(encoding="utf-8"))
            if meta is not None:
                _hash_field_in(report, meta, "draft_sha256", expected_review_hashes,
                               "finalized_draft_hash_mismatch", chunk_id, meta_path)
                if review_path.exists():
                    _hash_field_in(report, meta, "review_sha256", content_hashes(review_path.read_text(encoding="utf-8")),
                                   "finalized_review_hash_mismatch", chunk_id, meta_path)
        elif reviewed and meta is not None and meta.get("review_input_sha256"):
            base_path = root / "output" / "chunks" / f"{chunk_id}.zh-Hant.md"
            if not base_path.exists():
                _add(report, "reviewed_input_missing", "Reviewed artifact's input translation is missing", chunk_id, base_path)
                expected_review_hashes = set()
            else:
                base_hashes = content_hashes(base_path.read_text(encoding="utf-8"))
                if meta.get("review_input_sha256") not in base_hashes:
                    _add(report, "reviewed_review_input_hash_mismatch",
                         "Reviewed metadata does not match its input translation", chunk_id, meta_path,
                         {"actual": meta.get("review_input_sha256"), "valid_hashes": sorted(base_hashes)})
                expected_review_hashes = {str(meta.get("review_input_sha256"))}
        _hash_field_in(report, review, "translation_sha256", expected_review_hashes,
                       "review_translation_hash_mismatch", chunk_id, review_path)
        if finalized and review.get("verdict") not in {"pass", "revise"}:
            _add(report, "finalized_review_unresolved", "Finalized review must be pass or revise, not unresolved escalation",
                 chunk_id, review_path, {"actual": review.get("verdict")})
        elif not finalized and review.get("verdict") != "pass":
            _add(report, "review_not_passed", "Review verdict must be pass", chunk_id, review_path,
                 {"actual": review.get("verdict")})
        if not finalized and review.get("issues") != []:
            _add(report, "review_issues_not_empty", "Passed review must have an empty issues list", chunk_id, review_path,
                 {"actual": review.get("issues")})

    expected_input = quality.get("review_input_hashes", {}).get(chunk_id)
    if reviewed:
        provenance_hash = (meta or {}).get("input_hash") or (meta or {}).get("review_input_sha256")
        if not provenance_hash:
            _add(report, "reviewed_input_hash_missing",
                 "Reviewed translation requires input_hash or review_input_sha256 provenance", chunk_id, meta_path)
        elif expected_input and provenance_hash != expected_input:
            _add(report, "reviewed_input_hash_mismatch", "Reviewed translation was produced from a stale input", chunk_id,
                 meta_path, {"expected": expected_input, "actual": provenance_hash})
    elif expected_input and (meta is None or meta.get("input_hash") != expected_input):
        _add(report, "meta_input_hash_mismatch", "Translation metadata input hash is stale", chunk_id, meta_path,
             {"expected": expected_input, "actual": meta.get("input_hash") if meta else None})

    source_paragraphs = _paragraphs(source)
    target_paragraphs = _paragraphs(target)
    if len(source_paragraphs) != len(target_paragraphs) and not _paragraph_exception(
        quality.get("paragraph_count_exceptions", {}).get(chunk_id),
        len(source_paragraphs),
        len(target_paragraphs),
    ):
        _add(report, "paragraph_count_mismatch", "Source and target paragraph counts differ", chunk_id, target_path,
             {"source": len(source_paragraphs), "target": len(target_paragraphs)})

    _check_headings(chunk_id, source_paragraphs, target_paragraphs, target_path, quality, report)
    _check_target_text(chunk_id, target, target_path, quality, report)
    _check_terms(chunk_id, source, target, target_path, glossary, quality, report)

    marker = f"<!-- {chunk_id}; source pages {row.get('page_start')}-{row.get('page_end')} -->"
    expected_pieces.append(marker + "\n\n" + target.strip())


def _read_json_artifact(path: Path, kind: str, chunk_id: str, report: QualityReport) -> Optional[Dict[str, Any]]:
    if not path.exists():
        _add(report, f"{kind}_missing", f"Required {kind} artifact does not exist", chunk_id, path)
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        _add(report, f"{kind}_invalid", f"Cannot read {kind}: {exc}", chunk_id, path)
        return None
    if not isinstance(value, dict):
        _add(report, f"{kind}_invalid", f"{kind} must contain a JSON object", chunk_id, path)
        return None
    return value


def _hash_field(
    report: QualityReport,
    artifact: JsonMap,
    field_name: str,
    expected: str,
    code: str,
    chunk_id: str,
    path: Path,
) -> None:
    if artifact.get(field_name) != expected:
        _add(report, code, f"{field_name} does not match the chosen artifact", chunk_id, path,
             {"expected": expected, "actual": artifact.get(field_name)})


def _hash_field_in(
    report: QualityReport,
    artifact: JsonMap,
    field_name: str,
    expected: Set[str],
    code: str,
    chunk_id: str,
    path: Path,
) -> None:
    if artifact.get(field_name) not in expected:
        _add(report, code, f"{field_name} does not match the chosen artifact", chunk_id, path,
             {"valid_hashes": sorted(expected), "actual": artifact.get(field_name)})


def _paragraphs(text: str) -> List[str]:
    return [part.strip() for part in re.split(r"\n\s*\n", text.strip()) if part.strip()]


def _paragraph_exception(exception: Any, source_count: int, target_count: int) -> bool:
    if exception is True:
        return True
    if isinstance(exception, int) and not isinstance(exception, bool):
        return target_count == exception
    if isinstance(exception, Mapping):
        source_ok = exception.get("source", source_count) == source_count
        target_ok = exception.get("target", target_count) == target_count
        delta_ok = exception.get("delta", target_count - source_count) == target_count - source_count
        return source_ok and target_ok and delta_ok and bool(exception.get("reason"))
    return False


def _check_headings(
    chunk_id: str,
    source_paragraphs: List[str],
    target_paragraphs: List[str],
    path: Path,
    quality: JsonMap,
    report: QualityReport,
) -> None:
    if chunk_id in set(quality.get("chapter_heading_exceptions", [])):
        return
    source_headings = [part for part in source_paragraphs if SOURCE_CHAPTER_RE.fullmatch(part)]
    target_headings = [part for part in target_paragraphs if TARGET_HEADING_RE.fullmatch(part)]
    if len(source_headings) != len(target_headings):
        _add(report, "chapter_heading_count_mismatch", "Source and target chapter-heading counts differ", chunk_id, path,
             {"source": source_headings, "target": target_headings})
    if source_headings and (not source_paragraphs or source_paragraphs[0] != source_headings[0]):
        _add(report, "source_heading_position", "Chapter heading is not the first source paragraph", chunk_id, path)
    if target_headings and (not target_paragraphs or target_paragraphs[0] != target_headings[0]):
        _add(report, "target_heading_position", "Chapter heading is not the first target paragraph", chunk_id, path)


def _check_target_text(
    chunk_id: str,
    target: str,
    path: Path,
    quality: JsonMap,
    report: QualityReport,
) -> None:
    simplified_allowed = set(quality.get("simplified_allowlist", []))
    simplified = sorted({char for char in target if char in HIGH_CONFIDENCE_SIMPLIFIED and char not in simplified_allowed})
    if simplified:
        _add(report, "simplified_chinese", "High-confidence Simplified Chinese characters found", chunk_id, path,
             {"characters": simplified})

    placeholders = [match.group(0) for match in PLACEHOLDER_RE.finditer(target)]
    allowed_placeholders = set(quality.get("placeholder_allowlist", []))
    placeholders = [item for item in placeholders if item not in allowed_placeholders]
    if placeholders:
        _add(report, "placeholder", "Unresolved placeholder found", chunk_id, path,
             {"matches": sorted(set(placeholders))})

    english_allowed = set(quality.get("english_allowlist", []))
    english_runs = []
    for match in LATIN_RUN_RE.finditer(target):
        phrase = match.group(0)
        words = re.findall(r"[A-Za-z]+", phrase.casefold())
        if sum(word in COMMON_ENGLISH for word in words) >= 2 and phrase not in english_allowed:
            english_runs.append(phrase)
    if english_runs:
        _add(report, "english_residue", "Probable untranslated English prose found", chunk_id, path,
             {"matches": english_runs[:10]})

    bracket_error = _unbalanced_brackets(target)
    if bracket_error:
        _add(report, "unbalanced_quotes_or_brackets", bracket_error, chunk_id, path)
    emphasis_error = _unbalanced_emphasis(target)
    if emphasis_error:
        _add(report, "unbalanced_emphasis", emphasis_error, chunk_id, path)


def _unbalanced_brackets(text: str) -> Optional[str]:
    pairs = {"「": "」", "『": "』", "（": "）", "【": "】", "《": "》", "〈": "〉", "〔": "〕", "［": "］", "｛": "｝", "(": ")", "[": "]", "{": "}"}
    closing = {value: key for key, value in pairs.items()}
    stack: List[str] = []
    for char in text:
        if char in pairs:
            stack.append(char)
        elif char in closing:
            if not stack or stack.pop() != closing[char]:
                return f"Unexpected or mismatched closing mark: {char}"
    if stack:
        return f"Unclosed mark(s): {''.join(stack[-10:])}"
    for left, right in (("“", "”"), ("‘", "’")):
        if text.count(left) != text.count(right):
            return f"Unbalanced quotation marks: {left}{right}"
    return None


def _unbalanced_emphasis(text: str) -> Optional[str]:
    cleaned = re.sub(r"(?m)^\s*\*\s+\*\s+\*\s*$", "", text)
    cleaned = re.sub(r"<!--.*?-->", "", cleaned, flags=re.DOTALL)
    bold_markers = len(re.findall(r"(?<!\\)\*\*", cleaned))
    without_bold = re.sub(r"(?<!\\)\*\*", "", cleaned)
    italic_markers = len(re.findall(r"(?<!\\)\*", without_bold))
    if bold_markers % 2:
        return "Odd number of Markdown bold delimiters"
    if italic_markers % 2:
        return "Odd number of Markdown italic delimiters"
    return None


def _check_terms(
    chunk_id: str,
    source: str,
    target: str,
    path: Path,
    glossary: List[Dict[str, str]],
    quality: JsonMap,
    report: QualityReport,
) -> None:
    raw_exceptions = quality.get("glossary_exceptions", {}).get(chunk_id, [])
    exceptions = set(raw_exceptions) if not isinstance(raw_exceptions, Mapping) else set(raw_exceptions.keys())
    for row in glossary:
        source_term = row.get("source_term", "")
        target_term = row.get("target_term", "")
        flags = 0 if row.get("generated") == "true" else re.IGNORECASE
        source_matches = re.search(
            rf"(?<![A-Za-z0-9]){re.escape(source_term)}(?![A-Za-z0-9])",
            source,
            flags,
        ) is not None
        if (
            source_term
            and target_term
            and source_term not in exceptions
            and source_matches
            and target_term not in target
        ):
            _add(report, "approved_term_missing", "Approved glossary form is absent from target", chunk_id, path,
                 {"source_term": source_term, "expected_target": target_term})


def _check_assembly(
    root: Path,
    rows: List[Dict[str, Any]],
    expected_pieces: List[str],
    report: QualityReport,
) -> None:
    path = root / "output" / "book.zh-Hant.md"
    if not path.exists():
        _add(report, "assembled_book_missing", "Assembled book does not exist", path=path)
        return
    actual = path.read_text(encoding="utf-8")
    expected = "\n\n".join(expected_pieces) + ("\n" if expected_pieces else "")
    actual_markers = [match.groups() for match in MARKER_RE.finditer(actual)]
    expected_markers = [
        (str(row.get("chunk_id")), str(row.get("page_start")), str(row.get("page_end"))) for row in rows
    ]
    if actual_markers != expected_markers:
        _add(report, "assembled_marker_order_mismatch", "Chunk markers are missing, duplicated, or out of order", path=path,
             details={"expected": expected_markers, "actual": actual_markers})
    if actual != expected:
        _add(report, "assembled_content_mismatch", "Assembled book is not an exact rendering of chosen translations", path=path,
             details={"expected_sha256": sha256_text(expected), "actual_sha256": sha256_text(actual)})


def _add(
    report: QualityReport,
    code: str,
    message: str,
    chunk_id: Optional[str] = None,
    path: Optional[Path] = None,
    details: Optional[Dict[str, Any]] = None,
) -> None:
    report.issues.append(
        QualityIssue(code, message, chunk_id, str(path) if path is not None else None, details or {})
    )


__all__ = [
    "DEPENDENCY_STAGE",
    "HIGH_CONFIDENCE_SIMPLIFIED",
    "QualityGateError",
    "QualityIssue",
    "QualityReport",
    "STAGES",
    "artifact_input_hash",
    "assert_quality_gate",
    "changed_dependencies",
    "content_hashes",
    "invalidation_plan",
    "mark_stale",
    "pipeline_sha256_text",
    "run_quality_gate",
    "sha256_text",
    "stale_stages_for_changes",
]
