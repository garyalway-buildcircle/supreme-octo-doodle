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

    def test_task_list_decision_list_and_inline_image(self):
        document = {
            "type": "doc",
            "content": [
                {
                    "type": "taskList",
                    "content": [
                        {
                            "type": "taskItem",
                            "attrs": {"state": "DONE"},
                            "content": [{"type": "text", "text": "ship"}],
                        },
                        {
                            "type": "taskItem",
                            "attrs": {"state": "TODO"},
                            "content": [{"type": "text", "text": "wait"}],
                        },
                    ],
                },
                {
                    "type": "decisionList",
                    "content": [
                        {
                            "type": "decisionItem",
                            "attrs": {"state": "DECIDED"},
                            "content": [{"type": "text", "text": "use markdown"}],
                        }
                    ],
                },
                {
                    "type": "paragraph",
                    "content": [
                        {
                            "type": "mediaInline",
                            "attrs": {"type": "file", "id": "file-1", "alt": "shot"},
                        }
                    ],
                },
            ],
        }
        rendered = adf_to_markdown(document, media=lambda node: "![shot](1/shot.png)")
        self.assertIn("- [x] ship", rendered)
        self.assertIn("- [ ] wait", rendered)
        self.assertIn("- **Decision:** use markdown", rendered)
        self.assertIn("![shot](1/shot.png)", rendered)

    def test_toc_include_and_macro_label(self):
        document = {
            "type": "doc",
            "content": [
                {"type": "heading", "attrs": {"level": 2}, "content": [{"type": "text", "text": "Intro"}]},
                {"type": "extension", "attrs": {"extensionKey": "toc"}},
                {
                    "type": "bodiedExtension",
                    "attrs": {
                        "extensionKey": "note",
                        "parameters": {"macroParams": {"title": {"value": "Careful"}}},
                    },
                    "content": [
                        {"type": "paragraph", "content": [{"type": "text", "text": "Mind the gap"}]}
                    ],
                },
                {
                    "type": "extension",
                    "attrs": {"extensionKey": "include"},
                },
            ],
        }

        def include(node):
            if (node.get("attrs") or {}).get("extensionKey") == "include":
                return "Shared page"
            return None

        rendered = adf_to_markdown(document, include=include)
        self.assertIn("- Intro", rendered)
        self.assertIn("**note: Careful**", rendered)
        self.assertIn("Mind the gap", rendered)
        self.assertIn("Shared page", rendered)


if __name__ == "__main__":
    unittest.main()
