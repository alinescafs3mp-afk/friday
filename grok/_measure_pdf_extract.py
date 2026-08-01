# G22 / proposal #3 — PDF extract measurement
# THRESHOLD DECLARED BEFORE FETCH (frozen; do not retune after results):
# Among first 10 successfully downloaded public PDF bodies (HTTP 200, %PDF- magic
# or application/pdf, size>100B):
#   meaningful := success and chars>=200 and letter_ratio>=0.55 and long_words>=10
# PASS enable application/pdf  iff  meaningful >= 7 / 10
# FAIL do not enable           otherwise
# Network/404 rows are excluded from the denominator and replaced by next URL.

from __future__ import annotations

import re
import time
import urllib.request

from friday.documents import DocumentExtractor

URLS = [
    "https://www.w3.org/WAI/ER/tests/xhtml/testfiles/resources/pdf/dummy.pdf",
    "https://www.africau.edu/images/default/sample.pdf",
    "https://css4.pub/2015/textbook/somatosensory.pdf",
    "https://css4.pub/2017/newsletter/drylab.pdf",
    "https://www.adobe.com/support/products/enterprise/knowledgecenter/media/c4611_sample_explain.pdf",
    "https://bitcoin.org/bitcoin.pdf",
    "https://arxiv.org/pdf/1706.03762.pdf",
    "https://raw.githubusercontent.com/mozilla/pdf.js/ba2edeae/web/compressed.tracemonkey-pldi-09.pdf",
    "https://file-examples.com/storage/fe8c7eef0c6364f6c9504cc/2017/10/file-sample_150kB.pdf",
    "https://www.orimi.com/pdf-test.pdf",
    "https://www.learningcontainer.com/wp-content/uploads/2019/09/sample-pdf-file.pdf",
    "https://www.w3.org/WAI/WCAG21/Techniques/pdf/img/table-word.pdf",
    "https://unec.edu.az/application/uploads/2014/12/pdf-sample.pdf",
    "https://www.clickdimensions.com/links/TestPDFfile.pdf",
]


def letter_ratio(text: str) -> float:
    if not text:
        return 0.0
    letters = sum(1 for c in text if c.isalpha() or c.isspace())
    return letters / len(text)


def long_words(text: str) -> int:
    return sum(1 for w in re.findall(r"\w+", text, flags=re.UNICODE) if len(w) >= 4)


def main() -> None:
    extractor = DocumentExtractor()
    downloaded: list[dict] = []
    for url in URLS:
        if len(downloaded) >= 10:
            break
        row: dict = {"url": url}
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "FridayPDFMeasure/1.0"})
            with urllib.request.urlopen(req, timeout=25) as resp:
                body = resp.read(6_000_000)
                ctype = (resp.headers.get("Content-Type") or "").split(";", 1)[0].strip()
                code = resp.status
            if code != 200 or len(body) <= 100:
                print("SKIP http", code, len(body), url)
                continue
            if not (body[:5] == b"%PDF-" or "pdf" in ctype.lower()):
                print("SKIP not-pdf", ctype, url)
                continue
            t0 = time.time()
            doc = extractor.extract(body, "measure.pdf", "application/pdf")
            text = doc.text or ""
            row.update(
                {
                    "bytes": len(body),
                    "ctype": ctype,
                    "success": bool(doc.success),
                    "error": doc.error or "",
                    "chars": len(text),
                    "letter_ratio": round(letter_ratio(text), 3),
                    "long_words": long_words(text),
                    "preview": " ".join(text.split())[:160],
                    "elapsed": round(time.time() - t0, 2),
                }
            )
            meaningful = (
                row["success"]
                and row["chars"] >= 200
                and row["letter_ratio"] >= 0.55
                and row["long_words"] >= 10
            )
            row["meaningful"] = meaningful
            downloaded.append(row)
            flag = "OK" if meaningful else "FAIL"
            print(flag, row["chars"], row["letter_ratio"], row["long_words"], row["elapsed"], url)
            print(" ", row["preview"][:120])
        except Exception as exc:  # network only
            print("ERR", type(exc).__name__, str(exc)[:100], url)

    meaningful_n = sum(1 for r in downloaded if r.get("meaningful"))
    print("====")
    print("THRESHOLD: meaningful >= 7/10")
    print(f"DOWNLOADED: {len(downloaded)}")
    print(f"MEANINGFUL: {meaningful_n}/{len(downloaded)}")
    if len(downloaded) >= 10 and meaningful_n >= 7:
        verdict = "PASS enable PDF"
    elif len(downloaded) < 10:
        verdict = f"INCOMPLETE only {len(downloaded)} downloads"
    else:
        verdict = "FAIL do not enable"
    print("VERDICT:", verdict)
    for r in downloaded:
        print(int(bool(r.get("meaningful"))), r.get("chars"), r.get("letter_ratio"), r.get("long_words"), r["url"][:90])


if __name__ == "__main__":
    main()
