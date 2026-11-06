# 第一批审稿问题的离线复现

从仓库根目录运行下面一条命令。使用项目 Python 环境（macOS 示例为 `.venv/bin/python`）和已安装的仓库依赖。输入是文中虚构的报道；两个栏目只存在于内存中。测试替换模型应答，从真实的 `editorial.inspect` 进入，保留输出结构校验、证据回填、栏目检查和最终判定。无需旧聊天、历史稿、账号或服务。缺少解释器或依赖时记录环境阻塞，不把它当作业务结论。

```sh
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python - <<'PY'
import hashlib, json
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import jsonschema
from single_pass import composition, digest, editorial

headline = "甲线计划于12月试运行"
lead, detail, page_note = headline + "。", "公告称试运行首周每天开行4班。", "本文已被查看3324次"
content = "\n".join((lead, detail, page_note))
source = dict(material_id="m", role="reference", status="read", origin_ref=["r"],
              url="https://example.invalid/report", title=headline, content=content,
              content_sha256=hashlib.sha256(content.encode()).hexdigest())
item = dict(ref="r", title=headline, source_id="example")
entry = dict(entry_id="e", group="", heading=headline, body=detail)

def exercise(kind, case):
    line = dict(format=kind, format_policy=dict(min_items=1, target_items=1, max_items=1,
                shortage="hold", item_min_chars=0, item_max_chars=200))
    packet = dict(format=kind, line=line, items=[item], materials=[source])
    draft = dict(title=headline, body_html="<p>" + detail + "</p>")
    if kind == "digest":
        packet["entries"] = [dict(id="e", title=headline, refs=["r"])]
        draft = dict(title="科技简报", entries=[entry], markdown=digest.render([entry]))

    def answer(root, stage, schema, prompt, *args, **kwargs):
        assert stage == "production-adversarial-review"
        payload = json.loads(prompt.rsplit("\n", 1)[1])
        quotes = (payload["sources"][0]["content"] if kind == "single"
                  else payload["materials"][0]["sources"][0]["excerpts"])
        numbers = {text: int(ref) for ref, text in quotes.items()}
        wanted = [(0, page_note if case == "irrelevant" else lead)]
        if case != "missing":
            wanted.append((1, detail))
        facts = [dict(statement=i, excerpt=numbers[text]) for i, text in wanted if text in numbers]
        if kind == "single":
            keys = schema["properties"]["checks"]["properties"]
            value = dict(checks={k: dict(decision="pass", reason="模拟审稿通过") for k in keys},
                         issues=[], contribution="交代计划与班次", title_change="对照计划", fact_checks=facts)
        else:
            verdict = dict(decision="pass", reason="模拟审稿通过", issues=[], facts=facts, no_claim=[])
            value = dict(entries={"e": verdict}, edition=dict(verdict, facts=[], issues=[], no_claim=[0]))
        jsonschema.validate(value, schema)
        return value

    with TemporaryDirectory() as tmp, patch.object(composition, "_model", side_effect=answer):
        result = editorial.inspect(Path(tmp), {"packet": packet}, draft, None)
    issues = [(i.get("entry_id", ""), i.get("text", "")) for i in result["issues"]]
    return result["decision"], issues

for kind, case in [("digest","irrelevant"), ("digest","missing"), ("digest","valid"),
                   ("single","missing"), ("single","valid")]:
    decision, issues = exercise(kind, case)
    print(kind, case, decision, issues)
PY
```

| 输出行 | 此次审核时的实际值 | 修复后目标 |
|---|---|---|
| `digest irrelevant` · 标题只引用页面计数 | `pass`（误放） | `revise`，问题定位到条目 e 的标题 |
| `digest missing` · 正文首句没有依据 | `revise` | `revise`，问题仍定位到条目 e 的正文 |
| `digest valid` · 标题和正文各有对应原句 | `pass` | `pass` |
| `single missing` · 只核了标题，漏了正文的班次 | `pass`（误放） | `revise`，问题定位到含班次的句子 |
| `single valid` · 标题和正文各有对应原句 | `pass` | `pass` |

命令成功退出只说明回放执行完毕。修后须逐行对照目标，并检查 `issues` 的定位，再补做[主方案第一批](AI交互故障与最小修复方案.md)中的单位不同、正常转述、合法阅读量和多句共同支撑的对照。这五例不等于复现全部 59 个历史场景，也不能代替对任意新闻事实的语义审查。
