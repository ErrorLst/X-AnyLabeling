#!/usr/bin/env python3
"""X-AnyLabeling fork 契约自检（零第三方依赖）。

本文件是 docs/custom/contract.json 的唯一校验入口，只使用标准库
（ast / json / pathlib / os / re / sys），既不依赖 pytest，也不依赖
PyQt6 / numpy，因此可以用两种方式运行：

    python3 tests/custom/test_fork_contract.py
    python3 -m pytest -p no:cacheprovider tests/custom/test_fork_contract.py -v

它断言的是「契约与上游代码是否还对得上」：

* 每个 mounts[] 锚点在目标文件里 strip 后命中恰好 1 行（matcher=line 全等 /
  matcher=startswith 前缀）；命中 0 行是 FAIL，命中多行是 FAIL，命中 1 行但
  行号与快照不同只算 WARN（行号只是快照）。
* 每个 upstream 成员在 <文件>:<类> / <文件>:@module 作用域内可见，判定规则见
  下面的「AST 覆盖边界」。
* code / tests / docs / skills 里写的路径真实存在；FEATURES.md 覆盖每个功能。

AST 覆盖边界（决定什么能查、什么查不到）：

* 类方法：指定类 ClassDef 的直接子节点里的同名 def / async def。
* 类级信号与常量：ClassDef 体里的 Assign / AnnAssign 且目标 Name 同名。
* 实例属性：类内任意 self.<名> 访问（赋值与普通读取都算）。例外是
  self.<名>(...) 这种纯调用形式：调用只说明有人调它，不说明它存在，所以一个
  名字如果只剩 self.<名>() 调用而没有 def / 类级赋值，即判定为找不到（上游把
  方法改名后，残留的调用点不会掩盖这次改名）。
* 模块级函数 / 常量 / re-export：Module 体的 def / class / Assign，或
  ImportFrom 的 name / asname。
* 模块级 __getattr__ 动态名字：普通查找查不到；特例是存在 def __getattr__ 且
  其函数体内有字符串常量等于该名字（anylabeling/app_info.py）。
* 运行时容器字段（如 actions.delete_file）：契约用 {name, anchor, matcher}
  登记，AST 查不到但唯一锚点命中 => WARN，锚点不命中 => FAIL。
* 跨文件同名（多个文件都有 def load_file(）：只在 key 指定的文件与作用域里查。
* Qt 基类 API 不登记；MRO 更深的类只看 key 指定的那个类。

环境变量：

* XAL_CONTRACT_ROOT：覆盖仓库根（变异演练把影子树放在 /tmp 时使用）。
* XAL_CONTRACT_STRICT=1：WARN 升级为 FAIL（同步上游后使用）。
"""

from __future__ import annotations

import ast
import functools
import json
import os
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
CONTRACT_REL = "docs/custom/contract.json"
FEATURES_REL = "docs/custom/FEATURES.md"
MAP_REL = "docs/custom/MAP.md"
SKILL_ROOT_REL = ".dsh/skills"
ROOT_ENV = "XAL_CONTRACT_ROOT"
STRICT_ENV = "XAL_CONTRACT_STRICT"
FEATURE_BYTES_MAX = 2560
SIZE_BASE_BYTES = 1024

REQUIRED_FEATURE_FIELDS = (
    "summary",
    "code",
    "tests",
    "enter",
    "test_cmd",
    "mounts",
    "soft_mounts",
    "upstream",
)
REQUIRED_CONTEXT_FIELDS = (
    "docs",
    "self_check",
    "self_check_commands",
    "skill_root",
    "skills",
)
# 早期草案里被删掉的字段，出现即为回归。最后两个旧 skill_root 字段名拆开
# 拼写，避免与验收用的 grep 关键词撞车。
REMOVED_KEYS = (
    "check",
    "used_by",
    "qt_api",
    "semantic_contracts",
    "known_risks",
    "skill_root_" + "authoritative",
    "skill_root_" + "repo_mirror",
)

_PATH_SPAN = re.compile(r"`([^`\n]+)`")
_PATH_TOKEN = re.compile(r"^[.\w][\w./-]*$")
_LINE_SUFFIX = re.compile(r":\d+(?:-\d+)?$")


def total_bytes(feature_count):
    """返回 contract.json 的字节上限：1024 + 2560 * N。"""
    return SIZE_BASE_BYTES + FEATURE_BYTES_MAX * feature_count


TOTAL_BYTES = total_bytes


# --------------------------------------------------------------- 基础读取


def _root(root=None):
    """返回仓库根：显式参数 > XAL_CONTRACT_ROOT > 脚本位置。"""
    if root is not None:
        return Path(root)
    override = os.environ.get(ROOT_ENV)
    if override:
        return Path(override)
    return REPO_ROOT


@functools.lru_cache(maxsize=None)
def _lines(root, rel):
    """返回文件的按行切分结果（元组，可哈希，带缓存）。"""
    return tuple(
        (Path(root) / rel).read_text(encoding="utf-8").splitlines()
    )


@functools.lru_cache(maxsize=None)
def _tree(root, rel):
    """返回文件的 AST（带缓存）。"""
    return ast.parse((Path(root) / rel).read_text(encoding="utf-8"), rel)


@functools.lru_cache(maxsize=None)
def _contract(root):
    """返回解析后的 contract.json（带缓存）。"""
    return json.loads(_contract_text(root))


@functools.lru_cache(maxsize=None)
def _contract_text(root):
    """返回 contract.json 的原始文本（带缓存）。"""
    return (Path(root) / CONTRACT_REL).read_text(encoding="utf-8")


# ----------------------------------------------------------- 纯判定函数


def _match_lines(lines, anchor, matcher="line"):
    """在给定的行序列里找锚点，返回 1 起点的行号列表（纯函数）。"""
    wanted = anchor.strip()
    if matcher == "line":
        return [
            number
            for number, line in enumerate(lines, 1)
            if line.strip() == wanted
        ]
    if matcher == "startswith":
        return [
            number
            for number, line in enumerate(lines, 1)
            if line.strip().startswith(wanted)
        ]
    raise ValueError("unknown matcher: %s" % matcher)


def _match(root, rel, anchor, matcher="line"):
    """在 <仓库根>/<rel> 里找锚点，返回 1 起点的行号列表。"""
    return _match_lines(_lines(root, rel), anchor, matcher)


def _class_def(tree, name):
    """返回文件顶层同名的 ClassDef，找不到返回 None。"""
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == name:
            return node
    return None


def _class_member_ok(cls, name):
    """按「AST 覆盖边界」判断类作用域里是否存在该成员。"""
    if cls is None:
        return False
    for node in cls.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.name == name:
                return True
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == name:
                    return True
        if isinstance(node, ast.AnnAssign):
            target = node.target
            if isinstance(target, ast.Name) and target.id == name:
                return True
    called = {
        id(node.func)
        for node in ast.walk(cls)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }
    for node in ast.walk(cls):
        if not isinstance(node, ast.Attribute) or node.attr != name:
            continue
        value = node.value
        if not isinstance(value, ast.Name) or value.id != "self":
            continue
        if id(node) not in called:
            return True
    return False


def _module_member_ok(tree, name):
    """按「AST 覆盖边界」判断模块作用域里是否存在该符号。"""
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.name == name:
                return True
        if isinstance(node, ast.ClassDef) and node.name == name:
            return True
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == name:
                    return True
        if isinstance(node, ast.AnnAssign):
            target = node.target
            if isinstance(target, ast.Name) and target.id == name:
                return True
        if isinstance(node, ast.ImportFrom):
            for alias in node.names:
                if (alias.asname or alias.name) == name:
                    return True
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == "__getattr__":
            for inner in ast.walk(node):
                if isinstance(inner, ast.Constant) and inner.value == name:
                    return True
    return False


def _member_ok(tree, scope, member, anchor_hits=None):
    """返回 "ast" / "text" / None，见文件头的「AST 覆盖边界」。

    Args:
        tree: 目标文件的 AST。
        scope: 类名，或 "@module" 表示模块作用域。
        member: 字符串，或 {name, anchor, matcher} 字典。
        anchor_hits: 字典成员的文本锚点命中行数。

    Returns:
        "ast"（AST 命中）/ "text"（AST 查不到但唯一文本锚点命中）/ None。
    """
    name = member["name"] if isinstance(member, dict) else member
    if scope == "@module":
        found = _module_member_ok(tree, name)
    else:
        found = _class_member_ok(_class_def(tree, scope), name)
    if found:
        return "ast"
    if isinstance(member, dict) and anchor_hits == 1:
        return "text"
    return None


def _mount_problem(feature_id, mount, hits):
    """返回挂载点的问题文案，None 表示没问题。"""
    where = "%s %s:%s" % (feature_id, mount["file"], mount.get("line"))
    if not hits:
        return "FAIL mount anchor missing: %s anchor=%s" % (
            where,
            mount["anchor"],
        )
    if len(hits) > 1:
        return "FAIL anchor not unique (%d hits): %s anchor=%s" % (
            len(hits),
            where,
            mount["anchor"],
        )
    line = mount.get("line")
    if isinstance(line, int) and hits[0] != line:
        return "WARN line drift: %s expected %s found %s anchor=%s" % (
            mount["file"],
            line,
            hits[0],
            mount["anchor"],
        )
    return None


def _symbol_problem(feature_id, rel, scope, name, result):
    """返回上游依赖符号的问题文案，None 表示没问题。"""
    where = "%s %s::%s.%s" % (feature_id, rel, scope, name)
    if result == "ast":
        return None
    if result == "text":
        return "WARN symbol text-only: %s" % where
    return "FAIL symbol missing: %s" % where


def _feature_span(text, feature_id):
    """返回 features.<id> 在 contract.json 文本里的 [start, end) 偏移。

    用花括号配对扫描，字符串内的括号不算数；找不到返回 None。
    """
    marker = '\n    "%s": {' % feature_id
    start = text.find(marker)
    if start < 0:
        return None
    start += 1
    cursor = text.index("{", start)
    depth = 0
    in_string = False
    escaped = False
    for index in range(cursor, len(text)):
        char = text[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return (start, index + 1)
    return None


def _feature_bytes(text, feature_id):
    """返回 features.<id> 在 contract.json 里的真实字节数。

    度量的是这一节在文件里的文本区间（含键名行），因为数组在文件里是
    单行紧凑写的：json.dumps(indent=2) 会把成员数组摊成每项一行，得到的
    数字比文件里真实占用大 10% 左右，不适合当体积守卫。

    区间定位不到（contract.json 换了缩进排版、键值不再同行开 `{`）时返回
    None，调用方必须当成问题处理（见 _feature_size_problems），不得当成 0。
    """
    span = _feature_span(text, feature_id)
    if span is None:
        return None
    return len(text[span[0]:span[1]].encode("utf-8"))


def _feature_size_problems(text, feature_id):
    """返回单节体积守卫的问题文案（区间定位不到同样是 FAIL）。"""
    size = _feature_bytes(text, feature_id)
    if size is None:
        return [
            "FAIL size: features.%s 无法定位文本区间（contract.json 格式"
            "变化？每节需以 '\\n    \"<id>\": {' 开头）" % feature_id
        ]
    if size > FEATURE_BYTES_MAX:
        return [
            "FAIL size: features.%s %dB > %dB（压缩 summary/upstream，"
            "或把行为级描述移出 contract.json）"
            % (feature_id, size, FEATURE_BYTES_MAX)
        ]
    return []


def _size_text(size):
    """把单节字节数格式化成展示文本（区间定位不到时 size 是 None）。"""
    return "?" if size is None else "%dB" % size


def _feature_field_problems(feature_id, feature):
    """返回必填字段的问题文案（soft_mounts 允许为空数组）。"""
    problems = []
    for field in REQUIRED_FEATURE_FIELDS:
        if feature.get(field) is None:
            problems.append("features.%s 缺字段 %s" % (feature_id, field))
            continue
        if field == "soft_mounts":
            # 实例级包装不是每个功能都有：空数组 = 该功能没有软挂载。
            continue
        if feature[field] == []:
            problems.append("features.%s.%s 不能为空" % (feature_id, field))
    return problems


def _doc_paths(text):
    """返回文档里可以当仓库路径核对的片段（跳过通配与占位符）。"""
    found = set()
    for span in _PATH_SPAN.findall(text):
        token = _LINE_SUFFIX.sub("", span.strip()).rstrip("/")
        if not token or "/" not in token:
            continue
        if any(char in token for char in "*<>? "):
            continue
        if "..." in token or token.startswith("http"):
            continue
        if not _PATH_TOKEN.match(token):
            continue
        found.add(token)
    return sorted(found)


# --------------------------------------------------------------- 全量检查


@functools.lru_cache(maxsize=None)
def _problems_cached(root_str):
    """返回 (failures, warnings)，root_str 为仓库根的字符串形式。"""
    root = Path(root_str)
    try:
        contract = _contract(root)
    except Exception as error:  # noqa: BLE001
        return (["FAIL contract unreadable: %s" % error], [])

    features = contract.get("features")
    if not isinstance(features, dict) or not features:
        return (["FAIL contract features missing or empty"], [])

    problems = []
    for key in REMOVED_KEYS:
        if _has_key(contract, key):
            problems.append("FAIL removed field present: %s" % key)

    context = contract.get("agent_context") or {}
    for field in REQUIRED_CONTEXT_FIELDS:
        if not context.get(field):
            problems.append("FAIL agent_context.%s missing" % field)

    problems.extend(_mount_problems(root, features))
    problems.extend(_symbol_problems(root, features))
    problems.extend(_path_problems(root, features))
    problems.extend(_doc_problems(root, features))
    problems.extend(_skill_problems(root, context))
    problems.extend(_size_problems(root, features))

    unique = list(dict.fromkeys(problems))
    failures = [item for item in unique if item.startswith("FAIL ")]
    warnings = [item for item in unique if not item.startswith("FAIL ")]
    return (failures, warnings)


def _has_key(node, key):
    """递归判断 JSON 结构里是否出现某个键。"""
    if isinstance(node, dict):
        for name, value in node.items():
            if name == key or _has_key(value, key):
                return True
    elif isinstance(node, list):
        for value in node:
            if _has_key(value, key):
                return True
    return False


def _mount_problems(root, features):
    """检查所有挂载点锚点。"""
    problems = []
    for feature_id, feature in features.items():
        for mount in feature.get("mounts") or []:
            try:
                hits = _match(
                    root,
                    mount["file"],
                    mount["anchor"],
                    mount.get("matcher", "line"),
                )
            except Exception as error:  # noqa: BLE001
                problems.append(
                    "FAIL mount anchor missing: %s %s anchor=%s (%s)"
                    % (
                        feature_id,
                        mount.get("file"),
                        mount.get("anchor"),
                        error,
                    )
                )
                continue
            problem = _mount_problem(feature_id, mount, hits)
            if problem:
                problems.append(problem)
    return problems


def _symbol_problems(root, features):
    """检查所有上游依赖符号。"""
    problems = []
    for feature_id, feature in features.items():
        for key, members in (feature.get("upstream") or {}).items():
            rel, separator, scope = key.rpartition(":")
            if not separator:
                problems.append(
                    "FAIL upstream key malformed: %s %s" % (feature_id, key)
                )
                continue
            try:
                tree = _tree(root, rel)
            except Exception as error:  # noqa: BLE001
                problems.append(
                    "FAIL symbol missing: %s %s::%s (%s)"
                    % (feature_id, rel, scope, error)
                )
                continue
            if scope != "@module" and _class_def(tree, scope) is None:
                problems.append(
                    "FAIL class missing: %s %s::%s" % (feature_id, rel, scope)
                )
                continue
            for member in members:
                name = member["name"] if isinstance(member, dict) else member
                hits = None
                if isinstance(member, dict):
                    hits = _match(
                        root,
                        rel,
                        member["anchor"],
                        member.get("matcher", "startswith"),
                    )
                    if len(hits) > 1:
                        problems.append(
                            "FAIL anchor not unique (%d hits): %s %s "
                            "anchor=%s"
                            % (len(hits), feature_id, rel, member["anchor"])
                        )
                        continue
                hits_count = len(hits) if hits is not None else None
                result = _member_ok(tree, scope, member, hits_count)
                if result is None and isinstance(member, dict) and not hits:
                    problems.append(
                        "FAIL symbol anchor missing: %s %s anchor=%s"
                        % (feature_id, rel, member["anchor"])
                    )
                problem = _symbol_problem(
                    feature_id, rel, scope, name, result
                )
                if problem:
                    problems.append(problem)
    return problems


def _path_problems(root, features):
    """检查 features[].code / tests 里的路径。"""
    problems = []
    for feature_id, feature in features.items():
        for field in ("code", "tests"):
            for rel in feature.get(field) or []:
                if not (root / rel).exists():
                    problems.append(
                        "FAIL path missing: %s %s %s"
                        % (feature_id, field, rel)
                    )
    return problems


def _doc_problems(root, features):
    """检查文档覆盖与文档里出现的仓库路径。"""
    problems = []
    features_md = root / FEATURES_REL
    if not features_md.exists():
        problems.append("FAIL doc missing: %s" % FEATURES_REL)
    else:
        headings = set(
            re.findall(
                r"^##\s+(\S+)\s*$",
                features_md.read_text(encoding="utf-8"),
                re.MULTILINE,
            )
        )
        for feature_id in features:
            if feature_id not in headings:
                problems.append(
                    "FAIL doc does not cover feature: %s -> 期待 %s 里的 "
                    "## %s" % (feature_id, FEATURES_REL, feature_id)
                )
    for rel in (FEATURES_REL, MAP_REL):
        path = root / rel
        if not path.exists():
            problems.append("FAIL doc missing: %s" % rel)
            continue
        for token in _doc_paths(path.read_text(encoding="utf-8")):
            if not (root / token).exists():
                problems.append("FAIL doc path missing: %s %s" % (rel, token))
    return problems


def _skill_problems(root, context):
    """校验本仓库 .dsh/skills 下的 skill。"""
    problems = []
    skill_root = context.get("skill_root")
    if skill_root and skill_root != SKILL_ROOT_REL:
        problems.append(
            "FAIL skill_root unexpected: %s (期待 %s)"
            % (skill_root, SKILL_ROOT_REL)
        )
    for name in context.get("skills") or []:
        rel = "%s/%s/SKILL.md" % (SKILL_ROOT_REL, name)
        path = root / rel
        if not path.exists():
            problems.append("FAIL skill missing: %s" % rel)
            continue
        front = _front_matter(path.read_text(encoding="utf-8"))
        if front is None:
            problems.append("FAIL skill frontmatter missing: %s" % rel)
            continue
        if front.get("name") != name:
            problems.append(
                "FAIL skill name mismatch: %s name=%r (目录名 %s)"
                % (rel, front.get("name"), name)
            )
        if not front.get("description"):
            problems.append("FAIL skill description empty: %s" % rel)
    return problems


def _front_matter(text):
    """解析 SKILL.md 的 YAML frontmatter，返回扁平字典或 None。"""
    if not text.startswith("---"):
        return None
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return None
    front = {}
    for line in lines[1:]:
        if line.strip() == "---":
            return front
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        front[key.strip()] = value.strip().strip("'\"")
    return None


def _size_problems(root, features):
    """体积守卫：单节 <=2560B，全文件 <=1024+2560*N。"""
    problems = []
    text = _contract_text(root)
    for feature_id in sorted(features):
        problems.extend(_feature_size_problems(text, feature_id))
    total = (root / CONTRACT_REL).stat().st_size
    limit = total_bytes(len(features))
    if total > limit:
        problems.append(
            "FAIL size: %s %dB > %dB（%d 个功能的上限，考虑拆分或精简）"
            % (CONTRACT_REL, total, limit, len(features))
        )
    return problems


def _problems(root=None):
    """返回 (failures, warnings)，带缓存。"""
    failures, warnings = _problems_cached(str(_root(root)))
    return (list(failures), list(warnings))


_PRINTED = set()


def _report(root=None):
    """返回 (failures, warnings)，并把告警与体积摘要打印一次。"""
    resolved = _root(root)
    failures, warnings = _problems(resolved)
    key = str(resolved)
    if key not in _PRINTED:
        _PRINTED.add(key)
        for warning in warnings:
            print(warning)
        for line in _size_lines(resolved):
            print(line)
    return (failures, warnings)


def _size_lines(root):
    """返回体积摘要的文本行。"""
    try:
        features = _contract(root).get("features") or {}
        text = _contract_text(root)
    except Exception:  # noqa: BLE001
        return []
    lines = []
    for feature_id in sorted(features):
        lines.append(
            "SIZE features.%s %s (limit %dB)"
            % (
                feature_id,
                _size_text(_feature_bytes(text, feature_id)),
                FEATURE_BYTES_MAX,
            )
        )
    total = (Path(root) / CONTRACT_REL).stat().st_size
    lines.append(
        "SIZE %s %dB (limit %dB, N=%d)"
        % (CONTRACT_REL, total, total_bytes(len(features)), len(features))
    )
    return lines


def _strict():
    """返回是否处于严格模式。"""
    return os.environ.get(STRICT_ENV) == "1"


def _escalate(warnings):
    """严格模式下把 WARN 升级成 FAIL。"""
    if not _strict():
        return []
    return [
        "FAIL strict: %s" % warning[len("WARN "):] for warning in warnings
    ]


def _select(problems, *needles):
    """挑出含任一关键词的问题文案。"""
    return [
        item
        for item in problems
        if any(needle in item for needle in needles)
    ]


def _assert_clean(failures, warnings, *needles):
    """断言指定类别没有问题（严格模式下告警同样算失败）。"""
    picked_warnings = _select(warnings, *needles)
    messages = _select(failures, *needles) + _escalate(picked_warnings)
    assert not messages, "\n".join(messages)


# ------------------------------------------------------------------ 测试


def test_contract_file_is_valid_json():
    """contract.json 是合法 JSON 且顶层字段齐全。"""
    root = _root()
    contract = _contract(root)
    assert isinstance(contract, dict), "contract.json 顶层必须是对象"
    assert contract.get("schema_version") == 1, "schema_version 必须是 1"
    assert contract.get("kind") == "x-anylabeling-fork-contract", (
        "kind 不正确"
    )
    assert contract.get("repo") == "X-AnyLabeling", "repo 不正确"
    assert contract.get("verified_upstream_version"), (
        "verified_upstream_version 缺失"
    )
    assert isinstance(contract.get("features"), dict), "features 必须是对象"
    for key in REMOVED_KEYS:
        assert not _has_key(contract, key), "已删除的字段 %s 又出现了" % key
    failures, warnings = _report(root)
    _assert_clean(failures, warnings, "contract unreadable", "removed field")


def test_features_are_complete():
    """每个功能必填字段齐全、非空（soft_mounts 允许空数组）。"""
    root = _root()
    features = _contract(root).get("features") or {}
    assert features, "features 不能为空"
    for feature_id, feature in features.items():
        assert isinstance(feature, dict), "%s 必须是对象" % feature_id
        # soft_mounts 在 _feature_field_problems 里豁免「非空」要求。
        messages = _feature_field_problems(feature_id, feature)
        assert not messages, "\n".join(messages)
        for entry in feature["enter"]:
            assert entry.get("module") and entry.get("symbol"), (
                "features.%s.enter 条目缺 module/symbol" % feature_id
            )
        for mount in feature["mounts"]:
            for field in ("file", "line", "anchor", "matcher", "form"):
                assert mount.get(field) is not None, (
                    "features.%s.mounts 条目缺 %s" % (feature_id, field)
                )
            assert mount["matcher"] in ("line", "startswith"), (
                "features.%s.mounts matcher 不合法" % feature_id
            )
    failures, warnings = _report(root)
    _assert_clean(failures, warnings, "agent_context")


def test_contract_size_budget():
    """单节 <=2560B，全文件 <=1024+2560*N；超限或定位不到即失败。"""
    root = _root()
    features = _contract(root).get("features") or {}
    assert features, "features 不能为空"
    text = _contract_text(root)
    for feature_id in sorted(features):
        size = _feature_bytes(text, feature_id)
        print(
            "SIZE features.%s %s (limit %dB)"
            % (feature_id, _size_text(size), FEATURE_BYTES_MAX)
        )
        messages = _feature_size_problems(text, feature_id)
        assert not messages, "\n".join(messages)
    total = (root / CONTRACT_REL).stat().st_size
    limit = total_bytes(len(features))
    print(
        "SIZE %s %dB (limit %dB, N=%d)"
        % (CONTRACT_REL, total, limit, len(features))
    )
    assert total <= limit, "%s %dB 超过上限 %dB" % (CONTRACT_REL, total, limit)
    failures, warnings = _report(root)
    _assert_clean(failures, warnings, "size:")


def test_mount_point_anchors_present_and_unique():
    """每个挂载点锚点恰好命中 1 行。"""
    failures, warnings = _report()
    _assert_clean(
        failures, warnings, "mount anchor", "anchor not unique", "line drift"
    )


def test_upstream_dependency_symbols_exist():
    """每个上游依赖符号在指定文件与作用域里可见。"""
    failures, warnings = _report()
    _assert_clean(
        failures,
        warnings,
        "symbol missing",
        "symbol text-only",
        "symbol anchor",
        "class missing",
        "upstream key",
    )


def test_code_and_test_paths_exist():
    """features[].code / tests 里的路径都存在。"""
    failures, warnings = _report()
    _assert_clean(failures, warnings, "path missing")


def test_docs_cover_every_feature():
    """FEATURES.md 覆盖每个 feature id（## <id> 标题）。"""
    failures, warnings = _report()
    _assert_clean(failures, warnings, "doc does not cover feature")


def test_doc_paths_exist():
    """FEATURES.md / MAP.md 里出现的仓库路径都存在。"""
    failures, warnings = _report()
    _assert_clean(failures, warnings, "doc path missing", "doc missing")


def test_repo_skills_are_valid():
    """本仓库 .dsh/skills 下的 skill 存在且 frontmatter 合法。"""
    failures, warnings = _report()
    _assert_clean(failures, warnings, "skill")


def test_mount_line_drift_is_warning_only():
    """行号漂移只产生 WARN，绝不产生 FAIL。"""
    failures, _ = _report()
    drift_as_failure = _select(failures, "line drift")
    assert not drift_as_failure, "\n".join(drift_as_failure)


def test_checker_self_test_mount_rename():
    """合成数据：挂载点改名后必须报 mount anchor missing。"""
    lines = [
        "from anylabeling.custom.edit_extras import install_edit_extras",
        "        install_edit_extras(self)  # 滚轮缩放",
        "        install_smudge_tool(self)",
    ]
    hits = _match_lines(lines, "install_edit_extras(self)", "startswith")
    assert hits == [2], "锚点应命中第 2 行，实际 %s" % hits
    renamed = [
        lines[0],
        "        install_edit_extras_v2(self)  # 滚轮缩放",
        lines[2],
    ]
    renamed_hits = _match_lines(
        renamed, "install_edit_extras(self)", "startswith"
    )
    assert renamed_hits == [], "改名后不应再命中：%s" % renamed_hits
    mount = {
        "file": "anylabeling/views/labeling/label_widget.py",
        "line": 2,
        "anchor": "install_edit_extras(self)",
        "matcher": "startswith",
    }
    problem = _mount_problem("edit_extras", mount, renamed_hits)
    assert problem is not None and problem.startswith(
        "FAIL mount anchor missing"
    ), "改名后应报 mount anchor missing，实际 %r" % problem
    drift = _mount_problem("edit_extras", mount, [5])
    assert drift.startswith("WARN line drift"), "行号漂移应是 WARN：%r" % drift


def test_checker_self_test_symbol_rename():
    """合成数据：上游方法改名后必须报 symbol missing。"""
    source = (
        "class Canvas:\n"
        "    def offset_to_center(self):\n"
        "        return 1\n"
    )
    tree = ast.parse(source)
    assert _member_ok(tree, "Canvas", "offset_to_center") == "ast"
    renamed = ast.parse(
        source.replace("offset_to_center", "offset_to_center_v2")
    )
    result = _member_ok(renamed, "Canvas", "offset_to_center")
    assert result is None, "改名后不应再判定为存在：%r" % result
    problem = _symbol_problem(
        "smudge_tool", "canvas.py", "Canvas", "offset_to_center", result
    )
    assert problem.startswith("FAIL symbol missing"), (
        "改名后应报 symbol missing，实际 %r" % problem
    )
    call_only = ast.parse(
        "class Canvas:\n"
        "    def f(self):\n"
        "        return self.offset_to_center()\n"
    )
    assert _member_ok(call_only, "Canvas", "offset_to_center") is None, (
        "只剩 self.<名>() 调用时不应判定为存在"
    )
    read_only = ast.parse(
        "class Canvas:\n    def f(self):\n        return self.pixmap\n"
    )
    assert _member_ok(read_only, "Canvas", "pixmap") == "ast", (
        "self.<名> 普通读取应判定为存在"
    )
    text_only = _member_ok(
        ast.parse("class W:\n    pass\n"),
        "W",
        {"name": "delete_file", "anchor": "delete_file = action("},
        1,
    )
    assert text_only == "text", "唯一文本锚点命中应退回 text：%r" % text_only
    assert _symbol_problem("f", "m.py", "W", "delete_file", "text").startswith(
        "WARN symbol text-only"
    )


def test_checker_self_test_soft_mounts_may_be_empty():
    """合成数据：soft_mounts 允许为空，其它必填字段仍不能为空。"""
    feature = {field: "x" for field in REQUIRED_FEATURE_FIELDS}
    feature["soft_mounts"] = []
    assert _feature_field_problems("demo", feature) == [], (
        "soft_mounts 为空数组不应报问题"
    )
    feature["mounts"] = []
    problems = _feature_field_problems("demo", feature)
    assert problems == ["features.demo.mounts 不能为空"], (
        "其它必填字段为空仍应报问题，实际 %r" % problems
    )
    feature["mounts"] = "x"
    feature["soft_mounts"] = None
    problems = _feature_field_problems("demo", feature)
    assert problems == ["features.demo 缺字段 soft_mounts"], (
        "soft_mounts 整个缺字段仍应报问题，实际 %r" % problems
    )


def test_checker_self_test_size_span_missing():
    """合成数据：contract.json 换排版后体积守卫必须 FAIL，不得静默成 0。"""
    reindented = (
        "{\n"
        '    "features": {\n'
        '        "edit_extras":\n'
        "        {\n"
        '            "summary": "x"\n'
        "        }\n"
        "    }\n"
        "}\n"
    )
    assert _feature_span(reindented, "edit_extras") is None, (
        "键值不再同行开 { 时不应命中文本区间"
    )
    assert _feature_bytes(reindented, "edit_extras") is None, (
        "区间定位不到时必须返回 None，不能当成 0 字节"
    )
    problems = _feature_size_problems(reindented, "edit_extras")
    assert len(problems) == 1 and problems[0].startswith(
        "FAIL size: features.edit_extras 无法定位文本区间"
    ), "重排后应报 FAIL，实际 %r" % problems


TESTS = (
    test_contract_file_is_valid_json,
    test_features_are_complete,
    test_contract_size_budget,
    test_mount_point_anchors_present_and_unique,
    test_upstream_dependency_symbols_exist,
    test_code_and_test_paths_exist,
    test_docs_cover_every_feature,
    test_doc_paths_exist,
    test_repo_skills_are_valid,
    test_mount_line_drift_is_warning_only,
    test_checker_self_test_mount_rename,
    test_checker_self_test_symbol_rename,
    test_checker_self_test_soft_mounts_may_be_empty,
    test_checker_self_test_size_span_missing,
)


def main():
    """跑完所有 test_*，打印摘要，失败返回 1。"""
    root = _root()
    failures, warnings = _report(root)
    errors = []
    for test in TESTS:
        try:
            test()
        except AssertionError as error:
            errors.append((test.__name__, str(error)))
        except Exception as error:  # noqa: BLE001
            errors.append(
                (test.__name__, "%s: %s" % (type(error).__name__, error))
            )
    print("=" * 72)
    print("X-AnyLabeling fork contract self check")
    print("root   : %s" % root)
    print("strict : %s" % _strict())
    for warning in warnings:
        print(warning)
    for message in failures:
        print(message)
    for name, message in errors:
        print("TEST FAILED %s: %s" % (name, message))
    strict_failures = _escalate(warnings)
    ok = not failures and not errors and not strict_failures
    print(
        "RESULT: %s (%d failures, %d warnings, %d test errors)"
        % (
            "PASS" if ok else "FAIL",
            len(failures) + len(strict_failures),
            len(warnings),
            len(errors),
        )
    )
    print("=" * 72)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
