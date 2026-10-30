"""纯 Python BM25（Okapi，架构文档 v0.5 14.2）。

索引只落盘切块；posting list 加载时从切块重建，万级规模秒级。
"""

from __future__ import annotations

import math
from collections import Counter

from codingwiki.retrieval.tokenize import tokenize

DEFAULT_K1 = 1.5
DEFAULT_B = 0.75


class Bm25Index:
    """对一组文本建 BM25 索引并查询。rank 从 1 开始。"""

    def __init__(self, texts: list[str], k1: float = DEFAULT_K1, b: float = DEFAULT_B) -> None:
        self._k1 = k1
        self._b = b
        self._doc_tokens: list[list[str]] = [tokenize(t) for t in texts]
        self._doc_len = [len(t) for t in self._doc_tokens]
        self._avgdl = sum(self._doc_len) / len(self._doc_len) if self._doc_len else 0.0
        df: Counter[str] = Counter()
        self._tf: list[Counter[str]] = []
        for tokens in self._doc_tokens:
            tf = Counter(tokens)
            self._tf.append(tf)
            df.update(tf.keys())
        self._df = df
        self.vocab_size = len(df)

    def __len__(self) -> int:
        return len(self._doc_tokens)

    def _idf(self, term: str) -> float:
        n = len(self._doc_tokens)
        df = self._df.get(term, 0)
        # Robertson 平滑 idf，保证非负
        return math.log(1.0 + (n - df + 0.5) / (df + 0.5))

    def score(self, query: str, doc_index: int) -> float:
        if not self._doc_tokens:
            return 0.0
        total = 0.0
        tf = self._tf[doc_index]
        dl = self._doc_len[doc_index]
        for term in set(tokenize(query)):
            f = tf.get(term, 0)
            if f == 0:
                continue
            denom = f + self._k1 * (1 - self._b + self._b * dl / (self._avgdl or 1.0))
            total += self._idf(term) * f * (self._k1 + 1) / denom
        return total

    def query(self, query: str, top_n: int = 50) -> list[tuple[int, float]]:
        """返回 [(doc_index, score)] 按分数降序，零分不返回。"""
        scored = [(i, self.score(query, i)) for i in range(len(self._doc_tokens))]
        hits = [(i, s) for i, s in scored if s > 0.0]
        hits.sort(key=lambda x: (-x[1], x[0]))
        return hits[:top_n]


__all__ = ["Bm25Index"]
