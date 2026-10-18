"""Citation markers: the grammar, and everything that must not notice them."""
from __future__ import annotations

import random
import unittest

from steno import citations
from steno.citations import (
    apply,
    coerce,
    coverage,
    from_model,
    items,
    parse,
    refs,
    resolve,
    strip,
    to_refs,
    validate,
)
from steno.notes import parse_action_items
from steno.playback import format_offset

# The shapes real summaries come in: plain and bold-led bullets, a
# checkbox, a numbered and a starred item, owner headers (one with a
# name after it), a title, and blank lines.
SUMMARY = """# Vendor Pricing Call

## TL;DR
- They will send revised numbers.
- **Budget**: the cap stays at 10k.

## Decisions
- Go with the annual plan.

## Action items
**You**
- Send the usage figures by Friday
- [ ] Draft the comparison table

**Them (Alice)**
1. Revised quote next week

## Open questions
* Whether the discount applies retroactively

## Key facts
- Contract ends 2027-03-31.
"""
ITEM_TEXTS = [
    "They will send revised numbers.",
    "**Budget**: the cap stays at 10k.",
    "Go with the annual plan.",
    "Send the usage figures by Friday",
    "Draft the comparison table",
    "Revised quote next week",
    "Whether the discount applies retroactively",
    "Contract ends 2027-03-31.",
]

CITED_SHARE = 0.6  # of the items, in a random citing

SAMPLES = {
    "plain": SUMMARY,
    "crlf": SUMMARY.replace("\n", "\r\n"),
    "trailing spaces": SUMMARY.replace("numbers.", "numbers.   "),
    "line separator": SUMMARY.replace("10k.\n", "10k.\N{LINE SEPARATOR}"),
    "no final newline": SUMMARY.rstrip("\n"),
    "nested": "# T\n- a\n  - b\n    * c  \n",
    "no items": "# T\n\nThey agreed on Thursday.\n",
    "empty": "",
    "marked": SUMMARY.replace("annual plan.", "annual plan. <!--cite 5-9-->"),
}


class TestGrammar(unittest.TestCase):
    def test_reads_ranges_and_single_seconds(self):
        self.assertEqual(
            parse("- Pays by Friday <!--cite 201-221,460-->"),
            [(201, 221), (460, 460)],
        )

    def test_no_marker_no_cites(self):
        self.assertEqual(parse("- Pays by Friday"), [])

    def test_what_the_grammar_does_not_cover_is_left_alone(self):
        for line in (
            "x <!--cite -->",
            "x <!--cite 1-2-3-->",
            "x <!--cite 1,-->",
            "x <!--cite a-->",
            "x <!--CITE 1-->",
            "x <!--cite  1-->",
            "x <!-- cite 1-->",
            "x <!--cite 1--> and more",
            "x <!--cite 12345678-->",
        ):
            with self.subTest(line=line):
                self.assertEqual(parse(line), [])
                self.assertEqual(strip(line), line)

    def test_only_the_last_of_two_markers_counts(self):
        line = "x <!--cite 1--> <!--cite 2-->"
        self.assertEqual(parse(line), [(2, 2)])

    def test_a_pile_of_markers_is_stripped_whole(self):
        """Else one is left behind, and read as a cite."""
        for pile in (
            " <!--cite 1--> <!--cite 2-->",
            "<!--cite 1--><!--cite 2--><!--cite 3-->",
            " <!--cite 1-->  \t<!--cite 2-->  ",
        ):
            with self.subTest(pile=pile):
                self.assertEqual(strip("- x" + pile).rstrip(), "- x")
                self.assertEqual(strip(strip("- x" + pile)), strip("- x" + pile))
                self.assertEqual(items("- x" + pile)[0].text, "x")

    def test_a_marker_in_the_middle_of_the_line_is_not_the_end(self):
        line = "x <!--cite 1--> y <!--cite 2-->"
        self.assertEqual(strip(line), "x <!--cite 1--> y")

    def test_a_reversed_range_is_read_in_order(self):
        self.assertEqual(parse("x <!--cite 9-3-->"), [(3, 9)])

    def test_trailing_space_and_line_ending_do_not_hide_it(self):
        self.assertEqual(parse("x <!--cite 5-->  "), [(5, 5)])
        self.assertEqual(parse("x <!--cite 5-->\r\n"), [(5, 5)])


class TestStrip(unittest.TestCase):
    def test_takes_the_marker_and_the_space_before_it(self):
        self.assertEqual(strip("- a <!--cite 1-2-->\n- b\n"), "- a\n- b\n")

    def test_takes_a_marker_with_no_space_before_it(self):
        self.assertEqual(strip("- a<!--cite 1-->"), "- a")

    def test_takes_only_one_space(self):
        self.assertEqual(strip("- a   <!--cite 1-->"), "- a  ")

    def test_keeps_the_rest_of_the_line(self):
        self.assertEqual(strip("- a <!--cite 1-->  "), "- a  ")

    def test_every_line_ending_survives(self):
        sep = "\N{LINE SEPARATOR}"
        md = f"- a <!--cite 1-->\r\n- b <!--cite 2-->{sep}- c <!--cite 3-->\n"
        self.assertEqual(strip(md), f"- a\r\n- b{sep}- c\n")

    def test_a_marker_alone_leaves_an_empty_line(self):
        self.assertEqual(strip("a\n<!--cite 1-->\nb"), "a\n\nb")

    def test_without_a_marker_it_is_the_same_text(self):
        self.assertIs(strip(SUMMARY), SUMMARY)

    def test_twice_is_once(self):
        marked = SAMPLES["marked"]
        self.assertEqual(strip(strip(marked)), strip(marked))
        self.assertEqual(strip(marked), SUMMARY)


class TestItems(unittest.TestCase):
    def test_finds_the_bullets_in_order(self):
        self.assertEqual([i.text for i in items(SUMMARY)], ITEM_TEXTS)

    def test_headings_owners_and_title_are_not_items(self):
        lines = SUMMARY.splitlines()
        for item in items(SUMMARY):
            self.assertFalse(lines[item.line_no].startswith(("#", "**You")))
            self.assertNotIn("Alice", lines[item.line_no])

    def test_line_no_indexes_splitlines(self):
        lines = SUMMARY.splitlines()
        for item in items(SUMMARY):
            self.assertIn(item.text.split()[0], lines[item.line_no])

    def test_the_marker_is_read_and_is_not_text(self):
        md = "- a <!--cite 1-2,9-->\n- [x] b <!--cite 4-->\n- c\n"
        self.assertEqual(
            [(i.text, i.cites) for i in items(md)],
            [("a", [(1, 2), (9, 9)]), ("b", [(4, 4)]), ("c", [])],
        )

    def test_a_marker_alone_is_not_an_item(self):
        self.assertEqual(items("- <!--cite 1-->\n"), [])

    def test_coverage_counts_cited_items(self):
        md = apply(SUMMARY, {0: [(3, 4)], 5: [(7, 7)]})
        self.assertEqual(coverage(md), (8, 2))
        self.assertEqual(coverage(SUMMARY), (8, 0))
        self.assertEqual(coverage(""), (0, 0))


class TestApply(unittest.TestCase):
    def test_adds_a_marker_at_the_end_of_the_line(self):
        out = apply("- a\n- b\n", {1: [(5, 9), (30, 30)]})
        self.assertEqual(out, "- a\n- b <!--cite 5-9,30-->\n")

    def test_replaces_a_marker(self):
        out = apply("- a <!--cite 1-->\n", {0: [(2, 3)]})
        self.assertEqual(out, "- a <!--cite 2-3-->\n")

    def test_replacing_keeps_what_was_between_text_and_marker(self):
        out = apply("- a<!--cite 1-->\n", {0: [(2, 3)]})
        self.assertEqual(out, "- a<!--cite 2-3-->\n")

    def test_no_cites_removes_it(self):
        self.assertEqual(apply("- a <!--cite 1-->\n", {0: []}), "- a\n")

    def test_items_not_named_keep_their_markers(self):
        md = "- a <!--cite 1-->\n- b\n"
        self.assertEqual(
            apply(md, {1: [(2, 2)]}), "- a <!--cite 1-->\n- b <!--cite 2-->\n"
        )

    def test_a_pile_is_replaced_by_one_marker(self):
        out = apply("- a <!--cite 1--> <!--cite 2-->\n", {0: [(7, 8)]})
        self.assertEqual(out, "- a <!--cite 7-8-->\n")
        self.assertEqual(apply("- a <!--cite 1--><!--cite 2-->\n", {0: []}),
                         "- a\n")

    def test_an_item_that_is_not_there_is_an_error(self):
        with self.assertRaises(ValueError):
            apply("- a\n", {1: [(2, 2)]})

    def test_a_cite_the_grammar_cannot_hold_is_an_error(self):
        for bad in ((5, 3), (-1, 2), (0, 10**7)):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                apply("- a\n", {0: [bad]})

    def test_changes_nothing_but_the_marker(self):
        """Over every sample and a few hundred random citings: stripping
        gives the original back, the words are the same, the cites read
        back, and doing it twice is doing it once."""
        rng = random.Random(7)
        for name, md in SAMPLES.items():
            count = len(items(md))
            for _ in range(40):
                chosen = {
                    i: [
                        (a, a + rng.choice([0, 0, 3, 40]))
                        for a in (rng.randrange(0, 5000)
                                  for _ in range(rng.randrange(0, 4)))
                    ]
                    for i in range(count) if rng.random() < CITED_SHARE
                }
                with self.subTest(sample=name, chosen=chosen):
                    out = apply(md, chosen)
                    self.assertEqual(strip(out), strip(md))
                    self.assertEqual(apply(out, chosen), out)
                    self.assertEqual(
                        [i.text for i in items(out)],
                        [i.text for i in items(md)],
                    )
                    got = items(out)
                    for index, cites in chosen.items():
                        self.assertEqual(got[index].cites, cites)


class TestResolve(unittest.TestCase):
    OFFSETS = (0, 4, 4, 9, 20, 61)

    def test_a_range_covers_every_record_that_starts_in_it(self):
        self.assertEqual(resolve([(4, 9)], self.OFFSETS), [1, 2, 3])

    def test_the_ends_are_inside(self):
        self.assertEqual(resolve([(9, 20)], self.OFFSETS), [3, 4])

    def test_a_second_with_a_line_starting_in_it_means_all_of_them(self):
        self.assertEqual(resolve([(4, 4)], self.OFFSETS), [1, 2])

    def test_a_second_between_lines_means_the_one_being_spoken(self):
        self.assertEqual(resolve([(15, 15)], self.OFFSETS), [3])

    def test_two_lines_started_together_means_the_later_one(self):
        self.assertEqual(resolve([(5, 5)], self.OFFSETS), [2])

    def test_before_the_first_line_is_nothing(self):
        self.assertEqual(resolve([(2, 2)], [5, 9]), [])

    def test_a_range_with_no_line_in_it_falls_back_to_its_first_second(self):
        """A realign can empty a range; it must not click to nothing."""
        self.assertEqual(resolve([(25, 50)], self.OFFSETS), [4])

    def test_sorted_and_unique_across_cites(self):
        got = resolve([(61, 61), (0, 4), (4, 9), (0, 0)], self.OFFSETS)
        self.assertEqual(got, [0, 1, 2, 3, 5])

    def test_out_of_order_offsets_are_scanned_whole(self):
        offsets = [30, 10, 20, 12]
        self.assertEqual(resolve([(10, 20)], offsets), [1, 2, 3])
        self.assertEqual(resolve([(25, 25)], offsets), [2])

    def test_no_records_no_places(self):
        self.assertEqual(resolve([(1, 2)], []), [])

    def test_no_cites_no_places(self):
        self.assertEqual(resolve([], self.OFFSETS), [])


class TestValidate(unittest.TestCase):
    OFFSETS = tuple(range(0, 200, 5))  # 40 lines, five seconds apart

    def test_ids_outside_the_transcript_are_dropped(self):
        got = validate([(3, 3), (400, 400), (40, 41)], self.OFFSETS)
        self.assertEqual(got, [(3, 3)])

    def test_a_range_is_cut_to_the_part_inside(self):
        self.assertEqual(validate([(38, 55)], self.OFFSETS), [(38, 39)])

    def test_neighbours_become_a_range(self):
        got = validate([(1, 1), (2, 2), (3, 3), (7, 7)], self.OFFSETS)
        self.assertEqual(got, [(1, 3), (7, 7)])

    def test_overlaps_merge_and_order_is_by_line(self):
        got = validate([(10, 12), (2, 4), (11, 14), (5, 5)], self.OFFSETS)
        self.assertEqual(got, [(2, 5), (10, 14)])

    def test_a_reversed_range_is_read_in_order(self):
        self.assertEqual(validate([(5, 3)], self.OFFSETS), [(3, 5)])

    def test_six_cites_at_most(self):
        raw = [(i, i) for i in range(0, 20, 2)]
        got = validate(raw, self.OFFSETS)
        self.assertEqual(got, raw[:6])

    def test_ninety_seconds_is_the_widest_range(self):
        self.assertEqual(validate([(0, 18)], self.OFFSETS), [(0, 18)])
        self.assertEqual(
            validate([(0, 19)], self.OFFSETS), [(0, 0), (19, 19)]
        )

    def test_a_wide_range_costs_two_of_the_six(self):
        raw = [(0, 20)] + [(i, i) for i in range(25, 40, 2)]
        got = validate(raw, self.OFFSETS)
        self.assertEqual(got[:2], [(0, 0), (20, 20)])
        self.assertEqual(len(got), 6)

    def test_width_is_measured_on_the_offsets_not_the_ids(self):
        got = validate([(0, 3)], [0, 3, 100, 6])
        self.assertEqual(got, [(0, 0), (3, 3)])

    def test_no_transcript_no_cites(self):
        self.assertEqual(validate([(0, 3)], []), [])


class TestFromModel(unittest.TestCase):
    # Record i starts at OFFSETS[i] seconds into the meeting.
    OFFSETS = (0, 4, 9, 15, 62, 65, 70, 71)

    def test_tags_become_markers_in_seconds(self):
        out = from_model("- Pays Friday [L1-2, L7]\n", self.OFFSETS)
        self.assertEqual(out, "- Pays Friday <!--cite 4-9,71-->\n")

    def test_the_tags_are_gone_from_the_text(self):
        md = "- Pays Friday. [L1]\n- Sends it, [L2-3] then waits.\n"
        out = from_model(md, self.OFFSETS)
        self.assertNotIn("[L", out)
        self.assertEqual(
            [i.text for i in items(out)],
            ["Pays Friday.", "Sends it, then waits."],
        )

    def test_only_the_tags_and_their_space_leave_the_line(self):
        md = SUMMARY.replace("numbers.", "numbers. [L0-1]", 1)
        out = from_model(md, self.OFFSETS)
        self.assertEqual(strip(out), SUMMARY)
        self.assertEqual(items(out)[0].cites, [(0, 4)])

    def test_an_untagged_item_is_left_without_a_marker(self):
        out = from_model("- a [L1]\n- b\n", self.OFFSETS)
        self.assertEqual(out, "- a <!--cite 4-->\n- b\n")

    def test_ids_the_transcript_does_not_have_are_dropped(self):
        self.assertEqual(from_model("- a [L99]\n", self.OFFSETS), "- a\n")
        self.assertEqual(
            from_model("- a [L2, L99]\n", self.OFFSETS), "- a <!--cite 9-->\n"
        )

    def test_neighbouring_lines_make_one_cite(self):
        out = from_model("- a [L1, L2, L3]\n", self.OFFSETS)
        self.assertEqual(out, "- a <!--cite 4-15-->\n")

    def test_lines_in_one_second_make_one_cite(self):
        out = from_model("- a [L0] [L0]\n", [3, 4])
        self.assertEqual(out, "- a <!--cite 3-->\n")

    def test_the_cites_come_out_in_time_order(self):
        out = from_model("- a [L7, L1]\n", self.OFFSETS)
        self.assertEqual(out, "- a <!--cite 4,71-->\n")

    def test_a_passage_of_a_minute_stays_whole(self):
        out = from_model("- a [L3-4]\n", self.OFFSETS)
        self.assertEqual(out, "- a <!--cite 15-62-->\n")

    def test_a_sprawl_is_split_to_its_ends(self):
        out = from_model("- a [L0-2]\n", [0, 4, 100])
        self.assertEqual(out, "- a <!--cite 0,100-->\n")

    def test_out_of_order_offsets_still_give_a_low_to_high_cite(self):
        out = from_model("- a [L0-1]\n", [30, 10])
        self.assertEqual(out, "- a <!--cite 10-30-->\n")

    def test_a_tag_on_a_line_that_is_not_an_item_goes_nowhere(self):
        md = "# Title [L1]\n\n## Decisions [L2]\nA paragraph. [L3]\n- a\n"
        out = from_model(md, self.OFFSETS)
        self.assertEqual(out, "# Title\n\n## Decisions\nA paragraph.\n- a\n")

    def test_a_marker_the_model_wrote_is_not_trusted(self):
        out = from_model("- a <!--cite 1-9999-->\n- b [L2]\n", self.OFFSETS)
        self.assertEqual(out, "- a\n- b <!--cite 9-->\n")

    def test_markers_the_model_piled_up_are_all_gone(self):
        md = "- a <!--cite 1--> <!--cite 2-->\n- b [L2]<!--cite 3-->\n"
        out = from_model(md, self.OFFSETS)
        self.assertEqual(out, "- a\n- b <!--cite 9-->\n")

    def test_a_timestamp_the_marker_cannot_hold_is_no_cite(self):
        """One wild row must not make the whole summary raise."""
        offsets = [3, 10**9, 12, 20]
        for md, expected in (
            ("- a [L1]\n", "- a\n"),
            ("- a [L0-1]\n", "- a <!--cite 3-->\n"),
            ("- a [L0, L1, L2]\n", "- a <!--cite 3,12-->\n"),
            ("- a [L2-3]\n", "- a <!--cite 12-20-->\n"),
        ):
            with self.subTest(md=md):
                self.assertEqual(from_model(md, offsets), expected)
        self.assertEqual(from_model("- a [L0]\n", [-5]), "- a\n")

    def test_whatever_the_tags_say_the_result_is_clean(self):
        """Random ids, offsets and layouts: no error, no tag left, and
        the words are the model's."""
        rng = random.Random(11)
        pool = ["[L0]", "[L1-2]", "[L99]", "[L3, L1]", "[L2-1]", "[L0-9]",
                "[x]", ""]
        for _ in range(300):
            offsets = [rng.choice([0, 3, 40, 41, 10**9]) for _ in range(5)]
            body = [" ".join(rng.choices(pool, k=rng.randrange(4)))
                    for _ in range(4)]
            md = "".join(f"- item {n} {tags}\n" for n, tags in enumerate(body))
            with self.subTest(md=md, offsets=offsets):
                out = from_model(md, offsets)
                self.assertNotRegex(out, r"\[L\d")
                self.assertEqual(len(items(out)), 4)
                for n, item in enumerate(items(out)):
                    self.assertTrue(item.text.startswith(f"item {n}"))

    def test_spellings_models_use(self):
        dash = "\N{EN DASH}"
        for tag in (
            "[L1-2]", "[L1-L2]", f"[L1{dash}2]", "[l1-2]", "[L1 - 2]",
            "[L1, L2]", "[L1; L2]", "[L1 L2]", "[L1, 2]", "[ L1-2 ]",
        ):
            with self.subTest(tag=tag):
                out = from_model(f"- a {tag}\n", self.OFFSETS)
                self.assertEqual(out, "- a <!--cite 4-9-->\n")

    def test_a_tag_spelt_any_other_way_still_leaves_the_text(self):
        """The transcript shows a line as `[L1 00:04]`, and a model copies
        that, or words the tag its own way. A tag left in the text becomes
        the wording of a to-do, and its key."""
        for tag, cites in (
            ("[L1 00:04]", "4"),
            ("[L1, 00:04]", "4"),
            ("[L1 00:04, L2 00:09]", "4-9"),
            ("[Lines 1-2]", "4-9"),
            ("[line 1]", "4"),
            ("[L 1]", "4"),
            ("(L1)", "4"),
            ("(lines 1-2)", "4-9"),
            ("[L1-2, ]", "4-9"),
            ("[L1-2,]", "4-9"),
            ("[L1; L2.]", "4-9"),
            ("[L1 and L2]", "4-9"),
            ("[**L1**]", "4"),
            ("`[L1]`", "4"),
            ("[1]", "4"),
            ("[1-2]", "4-9"),
            ("[L1] [L99]", "4"),
        ):
            with self.subTest(tag=tag):
                out = from_model(f"- Send it {tag}\n", self.OFFSETS)
                self.assertEqual(out, f"- Send it <!--cite {cites}-->\n")

    def test_a_tag_with_no_line_in_it_is_gone_and_cites_nothing(self):
        for tag in ("[L99999999999]", "[L" + "9" * 5000 + "]", "[L99]"):
            with self.subTest(tag=tag[:20]):
                out = from_model(f"- Send it {tag}\n", self.OFFSETS)
                self.assertEqual(out, "- Send it\n")

    def test_a_stray_tag_never_reaches_a_to_do(self):
        md = "## Action items\n**You**\n- Send the figures [L1 00:04]\n"
        plain = "## Action items\n**You**\n- Send the figures\n"
        todo, = parse_action_items(from_model(md, self.OFFSETS))
        other, = parse_action_items(plain)
        self.assertEqual((todo.text, todo.key), (other.text, other.key))
        self.assertEqual(todo.cites, [(4, 4)])

    def test_tags_in_the_middle_are_found_too(self):
        out = from_model("- a [L1] and b [L3].\n", self.OFFSETS)
        self.assertEqual(out, "- a and b. <!--cite 4,15-->\n")

    def test_brackets_that_are_not_tags_stay(self):
        for line in (
            "- the [L2 cache is slow]",
            "- see [L2](https://example.com)",
            "- a [x] box and [done]",
            "- fix the (L2) cache before Monday",
            "- the [12] steps of the plan",
            "- moved to [12:30] on Friday",
            "- ask Bob (he knows [1] of them)",
            "- see [lines of code](https://example.com)",
            "- [L1 and the rest](notes)",
        ):
            with self.subTest(line=line):
                self.assertEqual(from_model(line + "\n", self.OFFSETS),
                                 line + "\n")

    def test_a_checkbox_is_not_a_tag(self):
        md = "- [ ] Send it [L1]\n- [x] Sent it\n"
        out = from_model(md, self.OFFSETS)
        self.assertEqual(out, "- [ ] Send it <!--cite 4-->\n- [x] Sent it\n")

    def test_a_reply_with_no_tags_is_the_reply(self):
        self.assertEqual(from_model(SUMMARY, self.OFFSETS), SUMMARY)

    def test_no_transcript_means_no_cites_and_no_tags(self):
        self.assertEqual(from_model("- a [L1]\n", []), "- a\n")

    def test_the_result_counts_as_cited(self):
        md = SUMMARY.replace("plan.", "plan. [L2]")
        md = md.replace("10k.", "10k. [L9]")
        self.assertEqual(coverage(from_model(md, self.OFFSETS)), (8, 1))


class TestRefs(unittest.TestCase):
    def test_the_first_second_of_each_cite(self):
        self.assertEqual(refs([(201, 221), (460, 460)]), "[03:21, 07:40]")

    def test_it_is_the_transcript_stamp(self):
        for second in (0, 59, 61, 3599, 3725):
            self.assertEqual(refs([(second, second)]),
                             f"[{format_offset(second)}]")

    def test_an_hour_in(self):
        self.assertEqual(refs([(3725, 3730)]), "[1:02:05]")

    def test_a_place_is_named_once(self):
        self.assertEqual(refs([(10, 12), (10, 10), (11, 11)]),
                         "[00:10, 00:11]")

    def test_nothing_to_point_at(self):
        self.assertEqual(refs([]), "")

    def test_a_marker_becomes_a_ref(self):
        md = "- a <!--cite 201-221,460-->\n- b\n"
        self.assertEqual(to_refs(md), "- a [03:21, 07:40]\n- b\n")

    def test_line_endings_and_other_lines_survive(self):
        md = "# T\r\n- a <!--cite 5-->\r\n- b  \r\n"
        self.assertEqual(to_refs(md), "# T\r\n- a [00:05]\r\n- b  \r\n")

    def test_a_marker_alone_is_just_the_ref(self):
        self.assertEqual(to_refs("<!--cite 5-->"), "[00:05]")

    def test_without_a_marker_it_is_the_same_text(self):
        self.assertIs(to_refs(SUMMARY), SUMMARY)

    def test_no_marker_is_left_over(self):
        out = to_refs(SAMPLES["marked"])
        self.assertNotIn("cite", out)
        self.assertIn("annual plan. [00:05]", out)


class TestCoerce(unittest.TestCase):
    def test_pairs_of_seconds_come_back_as_tuples(self):
        self.assertEqual(coerce([[1, 2], [5, 5], (7, 9)]),
                         [(1, 2), (5, 5), (7, 9)])

    def test_anything_else_is_dropped(self):
        junk = [[1], [1, 2, 3], ["a", "b"], [2, 1], [-1, 3], [1.5, 2],
                [True, 2], None, "ab", 7, {"a": 1, "b": 2}, [0, 10**7]]
        self.assertEqual(coerce([*junk, [3, 4]]), [(3, 4)])

    def test_not_a_list_is_nothing(self):
        for raw in (None, "1-2", 5, {"a": 1}):
            self.assertEqual(coerce(raw), [])

    def test_the_bound_is_the_grammar_s(self):
        self.assertEqual(coerce([[0, citations.MAX_SECOND]]),
                         [(0, citations.MAX_SECOND)])


if __name__ == "__main__":
    unittest.main()
