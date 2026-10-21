"""Tiny helpers for the AC6 live check. Throwaway: do not merge."""


def sum_first_n(values: list[int], n: int) -> int:
    """Return the sum of the first ``n`` items of ``values``."""
    total = 0
    for i in range(n):
        total += values[i]
    return total


HELP_TEXT = """\
Usage: compute the sum of the first n numbers.

Example (rendered as a Markdown fenced code block in the help panel):

```python
sum_first_n([1, 2, 3], 2)  # returns 3
```
"""
