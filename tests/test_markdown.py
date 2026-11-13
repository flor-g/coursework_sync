from scheduleaid.markdown import inline, to_blocks


def plain(block):
    body = block[block["type"]]
    return "".join(i["text"]["content"] if i["type"] == "text" else f"${i['equation']['expression']}$"
                   for i in body.get("rich_text", []))


def test_inline_formatting():
    items = inline("A **graph** is $G=(V,E)$, see [notes](https://x.edu/n.pdf) and `int x`.")
    kinds = [(i["type"], i.get("annotations"), (i.get("text") or {}).get("link")) for i in items]
    assert ("text", {"bold": True}, None) in kinds
    assert ("equation", None, None) in kinds
    assert ("text", None, {"url": "https://x.edu/n.pdf"}) in kinds
    assert ("text", {"code": True}, None) in kinds
    assert items[3]["equation"]["expression"] == "G=(V,E)"


def test_dollar_amounts_are_not_math():
    assert all(i["type"] == "text" for i in inline("costs $ 5 and $ 10"))


def test_blocks():
    md = """# Lecture 14: Graphs

Intro line one
continues here.

## Definitions
- vertex set
  - nested
- [x] done item
1. first

$$
\\sum_v \\deg(v) = 2|E|
$$

| Term | Meaning |
|---|---|
| deg | degree |

```c
int x = 1;
```

> Remember this
---
### Small heading
"""
    blocks = to_blocks(md)
    assert [b["type"] for b in blocks] == [
        "heading_2", "paragraph", "heading_3", "bulleted_list_item", "to_do", "numbered_list_item",
        "equation", "table", "code", "quote", "divider", "paragraph"]
    assert plain(blocks[1]) == "Intro line one\ncontinues here."
    assert plain(blocks[3]["bulleted_list_item"]["children"][0]) == "nested"
    assert blocks[4]["to_do"]["checked"] is True
    assert blocks[6]["equation"]["expression"] == "\\sum_v \\deg(v) = 2|E|"
    table = blocks[7]["table"]
    assert table["has_column_header"] is True and table["table_width"] == 2 and len(table["children"]) == 2
    assert blocks[8]["code"]["language"] == "c" and plain(blocks[8]) == "int x = 1;"
    assert blocks[11]["paragraph"]["rich_text"][0]["annotations"] == {"bold": True}


def test_single_line_display_math_and_unknown_code_language():
    blocks = to_blocks("$$x^2$$\n\n```weird\nfoo\n```")
    assert blocks[0]["equation"]["expression"] == "x^2"
    assert blocks[1]["code"]["language"] == "plain text"


def test_long_paragraph_is_split_within_notion_limits():
    (block,) = to_blocks("word " * 1000)
    assert all(len(i["text"]["content"]) <= 2000 for i in block["paragraph"]["rich_text"])


def test_figures():
    (block,) = to_blocks("![binary tree](https://x.edu/t.png)")
    item = block["paragraph"]["rich_text"][0]
    assert item["text"]["content"] == "[Figure: binary tree]" and item["annotations"] == {"italic": True}


def test_formatting_inside_bold_and_italic():
    from scheduleaid.markdown import inline

    def simple(text):
        return [(i["type"], (i.get("text") or {}).get("content") or i["equation"]["expression"],
                 i.get("annotations", {})) for i in inline(text)]

    assert simple("**`insert(x)`**: add") == [("text", "insert(x)", {"code": True, "bold": True}),
                                              ("text", ": add", {})]
    assert simple("*Step 1: $i = 5$.*") == [("text", "Step 1: ", {"italic": True}),
                                            ("equation", "i = 5", {"italic": True}),
                                            ("text", ".", {"italic": True})]
    assert simple("**bold *it* end**") == [("text", "bold ", {"bold": True}),
                                           ("text", "it", {"italic": True, "bold": True}),
                                           ("text", " end", {"bold": True})]
    assert "".join(c for _, c, _ in simple(r"costs \$5 \[Figure\]")) == "costs $5 [Figure]"
    link = inline("[**x**](https://a.b)")[0]
    assert link["annotations"] == {"bold": True} and link["text"]["link"] == {"url": "https://a.b"}
