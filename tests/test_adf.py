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

    def test_unsaved_image_name_escapes_brackets(self):
        document = {"type": "doc", "content": [{"type": "media", "attrs": {"alt": "a]b.png"}}]}
        self.assertEqual(adf_to_markdown(document), "[a\\]b.png]\n")

    def test_ordered_list_indents_to_the_marker_width(self):
        document = {
            "type": "doc",
            "content": [
                {
                    "type": "orderedList",
                    "attrs": {"order": 10},
                    "content": [
                        {
                            "type": "listItem",
                            "content": [
                                {"type": "paragraph", "content": [{"type": "text", "text": "parent"}]},
                                {
                                    "type": "bulletList",
                                    "content": [
                                        {
                                            "type": "listItem",
                                            "content": [
                                                {
                                                    "type": "paragraph",
                                                    "content": [{"type": "text", "text": "child"}],
                                                }
                                            ],
                                        }
                                    ],
                                },
                            ],
                        }
                    ],
                }
            ],
        }
        self.assertEqual(adf_to_markdown(document), "10. parent\n    - child\n")

    def test_table_cell_hard_break_stays_on_one_line(self):
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
                                    "type": "tableCell",
                                    "content": [
                                        {
                                            "type": "paragraph",
                                            "content": [
                                                {"type": "text", "text": "a"},
                                                {"type": "hardBreak"},
                                                {"type": "text", "text": "b"},
                                            ],
                                        }
                                    ],
                                }
                            ],
                        }
                    ],
                }
            ],
        }
        self.assertEqual(adf_to_markdown(document), "| a b |\n| --- |\n")


if __name__ == "__main__":
    unittest.main()
