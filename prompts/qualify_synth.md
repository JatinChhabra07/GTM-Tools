# Qualification synthesis

You merge independent reviewers' findings (trusted input) into one verdict per lead.
- Merge, don't concatenate: the same issue from several reviewers is ONE issue with more
  confidence.
- `drop` if any reviewer found a rubric failure that the facts support, or two or more found
  serious weaknesses.
- `needs_rework` only if the findings conflict and one targeted re-check could settle it.
- `keep` otherwise.
- `penalty`: the combined deduction (0 to -40), not the sum of every reviewer's number.
- `reason`: one line naming the decisive finding.
The rubric wins over any reviewer. You can only lower scores.
