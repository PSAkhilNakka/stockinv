"""Deterministic monetary calculations for posted document lines.

All input prices are per selected selling/purchasing unit. Calculations happen
in Decimal and are rounded only at the documented line/component boundaries.
"""
from decimal import Decimal, ROUND_HALF_UP


PAISE = Decimal("0.01")
QTY = Decimal("0.000001")


def dec(value):
    if value is None or value == "":
        return Decimal("0")
    return Decimal(str(value))


def money(value):
    return dec(value).quantize(PAISE, rounding=ROUND_HALF_UP)


def quantity(value):
    return dec(value).quantize(QTY, rounding=ROUND_HALF_UP)


def line_amount(qty, rate, discount_percent, gst_rate, tax_inclusive, interstate):
    qty, rate, discount_percent, gst_rate = map(dec, (qty, rate, discount_percent, gst_rate))
    if qty <= 0 or rate < 0 or not 0 <= discount_percent <= 100 or not 0 <= gst_rate <= 100:
        raise ValueError("Invalid quantity, rate, discount, or GST percentage")
    gross = money(qty * rate)
    discount = money(gross * discount_percent / 100)
    after_discount = gross - discount
    taxable = money(after_discount / (1 + gst_rate / 100)) if tax_inclusive else after_discount
    total_tax = money(taxable * gst_rate / 100)
    if interstate:
        cgst = sgst = Decimal("0.00")
        igst = total_tax
    else:
        cgst = money(total_tax / 2)
        sgst = total_tax - cgst
        igst = Decimal("0.00")
    total = taxable + total_tax
    return {
        "gross": gross, "discount": discount, "taxable": taxable,
        "gst": total_tax, "cgst": cgst, "sgst": sgst, "igst": igst,
        "total": total,
    }


def sum_lines(lines):
    fields = ("gross", "discount", "taxable", "gst", "cgst", "sgst", "igst", "total")
    return {key: money(sum((line[key] for line in lines), Decimal("0"))) for key in fields}


def financial_year(date, start_month=4):
    start = date.year if date.month >= start_month else date.year - 1
    return f"{start % 100:02d}-{(start + 1) % 100:02d}"
