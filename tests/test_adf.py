import unittest

from tools.adf import adf_to_markdown


class AdfToMarkdownTests(unittest.TestCase):
    def test_marks_mentions_lists_and_code(self):
        document = {
            "type": "doc",
            "content": [
                {
                    "type": "paragraph",
                    "content": [
                        {"type": "text", "text": "See "},
                        {
                            "type": "text",
                            "text": "this",
                            "marks": [
                                {"type": "strong"},
                                {"type": "link", "attrs": {"href": "https://example.com/a_b"}},
                            ],
                        },
                        {"type": "text", "text": " and "},
                        {"type": "mention", "attrs": {"text": "@Ada"}},
                    ],
                },
                {
                    "type": "bulletList",
                    "content": [
                        {
                            "type": "listItem",
                            "content": [
                                {"type": "paragraph", "content": [{"type": "text", "text": "one"}]}
                            ],
                        },
                        {
                            "type": "listItem",
                            "content": [
                                {"type": "paragraph", "content": [{"type": "text", "text": "two"}]},
                                {
                                    "type": "bulletList",
                                    "content": [
                                        {
                                            "type": "listItem",
                                            "content": [
                                                {
                                                    "type": "paragraph",
                                                    "content": [{"type": "text", "text": "nested"}],
                                                }
                                            ],
                                        }
                                    ],
                                },
                            ],
                        },
                    ],
                },
                {
                    "type": "codeBlock",
                    "attrs": {"language": "python"},
                    "content": [{"type": "text", "text": "print('hi')"}],
                },
            ],
        }
        self.assertEqual(
            adf_to_markdown(document),
            "\n".join(
                [
                    "See [**this**](https://example.com/a_b) and @Ada",
                    "",
                    "- one",
                    "- two",
                    "  - nested",
                    "",
                    "```python",
                    "print('hi')",
                    "```",
                    "",
                ]
            ),
        )

    def test_table_uses_the_first_row_as_the_header(self):
        document = {
            "type": "doc",
            "content": [
                {
                    "type": "table",
                    "content": [
                        {
                            "type": "tableRow",
                            "content": [
                                {
                                    "type": "tableHeader",
                                    "content": [
                                        {"type": "paragraph", "content": [{"type": "text", "text": "Name"}]}
                                    ],
                                },
                                {
                                    "type": "tableHeader",
                                    "content": [
                                        {"type": "paragraph", "content": [{"type": "text", "text": "A|B"}]}
                                    ],
                                },
                            ],
                        },
                        {
                            "type": "tableRow",
                            "content": [
                                {
                                    "type": "tableCell",
                                    "content": [
                                        {"type": "paragraph", "content": [{"type": "text", "text": "Ada"}]}
                                    ],
                                },
                                {
                                    "type": "tableCell",
                                    "content": [
                                        {"type": "paragraph", "content": [{"type": "text", "text": "1"}]}
                                    ],
                                },
                            ],
                        },
                    ],
                }
            ],
        }
        self.assertEqual(
            adf_to_markdown(document),
            "| Name | A\\|B |\n| --- | --- |\n| Ada | 1 |\n",
        )

    def test_empty_description(self):
        self.assertEqual(adf_to_markdown(None), "")

    def test_media_callback_replaces_the_placeholder(self):
        document = {"type": "doc", "content": [{"type": "media", "attrs": {"alt": "shot.png"}}]}
        self.assertEqual(adf_to_markdown(document), "[shot.png]\n")
        rendered = adf_to_markdown(document, lambda node: "![shot.png](PROJ-1/shot.png)")
        self.assertEqual(rendered, "![shot.png](PROJ-1/shot.png)\n")


if __name__ == "__main__":
    unittest.main()
