"""Stand-in for ``claude -p``: one paragraph per segment, 就是/那个 removed."""

import json
import sys

prompt = sys.stdin.read()
payload = prompt.split("<<<BLOCKS", 1)[1].split("BLOCKS>>>", 1)[0]
segments = json.loads(payload)
out = [
    {
        "from": s["id"],
        "text": s["text"].replace("就是", "").replace("那个", ""),
        "unsure": [],
    }
    for s in segments
]
print("好的，结果如下：\n" + json.dumps(out, ensure_ascii=False))
