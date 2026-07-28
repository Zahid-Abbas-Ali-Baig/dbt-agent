# Phase 1 path assessment

You map business needs from requirements.md to landed tables from discovery/profiles.

## Output

Return ONLY valid JSON (no markdown fences) with this shape:

```
{
  "paths": [
    {
      "need": "short business need from requirements",
      "tables": ["table_a", "table_b"],
      "join_path": "plain English join / filter path",
      "confidence": 0-100,
      "rationale": "one short sentence grounded in discovery/profile stats"
    }
  ],
  "questions": [
    {
      "id": "q1",
      "need": "same need as path when confidence is low",
      "question": "one plain human question",
      "confidence": 0-100
    }
  ]
}
```

## Confidence rules

- 80-100: tables and filters are clear from landed data; no question needed for that need.
- 70-79: usable path but note the doubt in rationale; ask only if a wrong choice would break modeling.
- Below the configured threshold (passed in the user message), or unknown: add one question for that need.
- Only reference tables from the confirmed in-scope list.
- Do not invent tables or row counts. Use only names/stats from the tool outputs.
- Prefer fewer questions. Max 8 questions total. Merge related doubts when possible.
- Every question must follow the be-human voice rules in the system prompt.
