"""A dependency-light PDF fallback for local installs without ReportLab."""
from __future__ import annotations

from io import BytesIO
from textwrap import shorten, wrap


def table_pdf(title, headers, rows, subtitle_lines=(), footer_lines=(), landscape=False):
    from matplotlib.backends.backend_pdf import PdfPages
    from matplotlib.figure import Figure

    output = BytesIO()
    rows = [list(row.values()) if isinstance(row, dict) else list(row) for row in rows]
    page_size = (11.69, 8.27) if landscape else (8.27, 11.69)
    per_page = 25 if landscape else 32
    pages = [rows[i:i + per_page] for i in range(0, len(rows), per_page)] or [[]]
    with PdfPages(output) as pdf:
        for page_no, page_rows in enumerate(pages, 1):
            fig = Figure(figsize=page_size)
            fig.patch.set_facecolor("white")
            fig.text(0.05, 0.95, title, fontsize=16, fontweight="bold", color="#16324f", va="top")
            lines = list(subtitle_lines)
            for idx, line in enumerate(lines[:5]):
                fig.text(0.05, 0.92 - idx * 0.023, str(line), fontsize=8, color="#425466", va="top")
            top = 0.89 - min(len(lines), 5) * 0.023
            ax = fig.add_axes([0.04, 0.08, 0.92, top - 0.1])
            ax.axis("off")
            if headers:
                max_chars = max(10, int((130 if landscape else 100) / len(headers)))
                text_rows = [[shorten(str(value if value is not None else ""), width=max_chars, placeholder="…")
                              for value in row] for row in page_rows]
                table = ax.table(cellText=text_rows, colLabels=[str(h) for h in headers],
                                 loc="upper center", cellLoc="left", colLoc="left")
                table.auto_set_font_size(False)
                table.set_fontsize(6.5 if landscape else 7)
                table.scale(1, 1.35)
                for (r, c), cell in table.get_celld().items():
                    cell.set_edgecolor("#dbe4eb")
                    cell.set_linewidth(0.4)
                    if r == 0:
                        cell.set_facecolor("#16324f")
                        cell.get_text().set_color("white")
                        cell.get_text().set_fontweight("bold")
                    elif r % 2 == 0:
                        cell.set_facecolor("#f5f8fa")
            if page_no == len(pages):
                footer_start = max(0.085, top - 0.025 * (len(page_rows) + 1) - 0.045)
                for idx, line in enumerate(list(footer_lines)[:4]):
                    fig.text(0.05, footer_start - idx * 0.021, str(line), fontsize=8, color="#16324f")
            fig.text(0.95, 0.03, f"Page {page_no} of {len(pages)}", ha="right", fontsize=7, color="#66788a")
            pdf.savefig(fig)
    return output.getvalue()
