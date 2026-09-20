from __future__ import annotations

import re

import httpx

FINETUNED_SYSTEM_PROMPT = (
    "You are a SQLite expert. Given a database schema and a question, "
    "write a single, syntactically correct SQLite query that answers the question. "
    "Output ONLY the SQL query, no explanation, no markdown fences.\n\n"
    "Schema:\n{schema}"
)

SUPERIOR_SYSTEM_PROMPT = (
    "You are an expert SQL engineer specializing in SQLite. You are given a database "
    "schema and a natural language question. Your job is to write a single, precise, "
    "syntactically correct SQLite query that answers the question.\n\n"
    "Rules:\n"
    "1. Output ONLY the raw SQL query — no markdown, no explanation, no commentary.\n"
    "2. Use only tables and columns that exist in the provided schema.\n"
    "3. Prefer explicit JOINs over implicit joins.\n"
    "4. Use aliases for readability when joining multiple tables.\n"
    "5. If the question is ambiguous, make the most reasonable interpretation.\n\n"
    "Schema:\n{schema}"
)

RETRY_PROMPT = (
    "Your previous SQL query failed.\n\n"
    "Previous query:\n```sql\n{previous_sql}\n```\n\n"
    "Error:\n{error}\n\n"
    "Write a corrected SQL query. Output ONLY the SQL, nothing else."
)

JUDGE_SYSTEM_PROMPT = (
    "You are a SQL quality judge. Given a natural language question, a database schema, "
    "a generated SQL query, and its execution results, determine if the query correctly "
    "answers the question.\n\n"
    "Respond with a JSON object containing:\n"
    '- "correct": true/false\n'
    '- "reason": brief explanation of why it is correct or incorrect\n'
    '- "suggestion": if incorrect, a brief suggestion for fixing it\n\n'
    "Output ONLY the JSON, nothing else."
)


def _clean_sql(text: str) -> str:
    text = text.strip()
    text = re.sub(r"^```(?:sql)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    text = text.strip()
    if ";" in text:
        text = text.split(";")[0] + ";"
    return text.strip()


def _normalize_endpoint(url: str) -> str:
    url = url.rstrip("/")
    if url.endswith("/chat/completions"):
        return url
    if url.endswith("/v1"):
        return url + "/chat/completions"
    return url + "/v1/chat/completions"


class LLMClient:
    def __init__(
        self,
        endpoint: str,
        token: str = "",
        model: str | None = None,
        timeout: float = 120.0,
    ) -> None:
        self.endpoint = _normalize_endpoint(endpoint)
        self.token = token
        self.model = model
        self.timeout = timeout
        self.total_input_tokens = 0
        self.total_output_tokens = 0

    def _estimate_tokens(self, text: str | None) -> int:
        if not text:
            return 0
        return int(len(text.split()) * 2.0)

    async def generate(
        self,
        messages: list[dict[str, str]],
        temperature: float = 0.0,
        max_tokens: int = 512,
    ) -> str:
        headers: dict[str, str] = {"Content-Type": "application/json"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"

        body: dict = {
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "stream": False,
        }
        if self.model:
            body["model"] = self.model

        # Estimate input tokens
        input_text = " ".join(m.get("content") or "" for m in messages)
        input_tokens = self._estimate_tokens(input_text)
        self.total_input_tokens += input_tokens

        async with httpx.AsyncClient(timeout=self.timeout) as client:
            resp = await client.post(self.endpoint, json=body, headers=headers)
            resp.raise_for_status()
            data = resp.json()

        output_text = data["choices"][0]["message"].get("content") or ""
        output_tokens = self._estimate_tokens(output_text)
        self.total_output_tokens += output_tokens

        return output_text

    async def generate_sql(
        self,
        question: str,
        schema: str,
        system_prompt: str,
        previous_sql: str | None = None,
        error: str | None = None,
    ) -> str:
        messages = [
            {"role": "system", "content": system_prompt.format(schema=schema)},
        ]

        if previous_sql and error:
            messages.append({"role": "user", "content": question})
            messages.append({"role": "assistant", "content": previous_sql})
            messages.append(
                {
                    "role": "user",
                    "content": RETRY_PROMPT.format(
                        previous_sql=previous_sql, error=error
                    ),
                }
            )
        else:
            messages.append({"role": "user", "content": question})

        raw = await self.generate(messages)
        return _clean_sql(raw)

    async def judge_sql(
        self,
        question: str,
        schema: str,
        sql: str,
        result: dict,
    ) -> dict:
        import json

        content = (
            f"Question: {question}\n\n"
            f"Schema:\n{schema}\n\n"
            f"Generated SQL:\n```sql\n{sql}\n```\n\n"
            f"Execution Result:\n{json.dumps(result, indent=2)}"
        )

        messages = [
            {"role": "system", "content": JUDGE_SYSTEM_PROMPT},
            {"role": "user", "content": content},
        ]

        raw = await self.generate(messages, temperature=0.0, max_tokens=512)
        raw = raw.strip()
        raw = re.sub(r"^```(?:json)?\s*", "", raw)
        raw = re.sub(r"\s*```$", "", raw)

        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return {"correct": False, "reason": raw, "suggestion": ""}

    async def ping(self) -> bool:
        try:
            headers: dict[str, str] = {"Content-Type": "application/json"}
            if self.token:
                headers["Authorization"] = f"Bearer {self.token}"
            body = {
                "messages": [{"role": "user", "content": "ping"}],
                "max_tokens": 1,
                "temperature": 0,
                "stream": False,
            }
            if self.model:
                body["model"] = self.model
            async with httpx.AsyncClient(timeout=30.0) as client:
                resp = await client.post(self.endpoint, json=body, headers=headers)
                return resp.status_code == 200
        except (httpx.HTTPError, httpx.TimeoutException):
            return False

    def get_token_usage(self) -> dict:
        return {
            "input": self.total_input_tokens,
            "output": self.total_output_tokens,
            "total": self.total_input_tokens + self.total_output_tokens,
        }
