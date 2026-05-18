from cocoon.search import rank, tokenize


def test_tokenize_lowercases_and_splits_ascii() -> None:
    assert tokenize("Hello, World 42!") == ["hello", "world", "42"]


def test_empty_inputs_return_zero_scores() -> None:
    assert rank("", ["doc"]) == [0.0]
    assert rank("query", []) == []


def test_term_overlap_ranks_higher_than_no_overlap() -> None:
    scores = rank("create issue", [
        "create a new issue",          # match both terms
        "delete a project",            # match neither
    ])
    assert scores[0] > 0
    assert scores[1] == 0
    assert scores[0] > scores[1]


def test_rarer_terms_outscore_common_ones() -> None:
    docs = [
        "linear issues create",
        "linear issues list",
        "linear teams list",
        "linear comments create",
    ]
    # "linear" appears in every doc; "comments" only in one. Doc with the rare
    # term should outrank docs that match only the common term.
    scores = rank("linear comments", docs)
    ranked = sorted(range(len(docs)), key=lambda i: -scores[i])
    assert ranked[0] == 3
