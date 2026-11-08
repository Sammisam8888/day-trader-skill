const inr = new Intl.NumberFormat("en-IN", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
const inr0 = new Intl.NumberFormat("en-IN", { maximumFractionDigits: 0 });

export const n = (x: unknown): number => (x == null || x === "" ? NaN : Number(x));
export const money = (x: unknown) => (Number.isFinite(n(x)) ? `₹${inr.format(n(x))}` : "—");
export const money0 = (x: unknown) => (Number.isFinite(n(x)) ? `₹${inr0.format(n(x))}` : "—");
export const price = (x: unknown) => (Number.isFinite(n(x)) && n(x) !== 0 ? inr.format(n(x)) : "—");
export const signed = (x: unknown) => {
  const v = n(x);
  if (!Number.isFinite(v)) return "—";
  return `${v > 0 ? "+" : v < 0 ? "−" : ""}₹${inr.format(Math.abs(v))}`;
};
export const pct = (x: unknown, d = 2) => {
  const v = n(x);
  if (!Number.isFinite(v)) return "—";
  return `${v > 0 ? "+" : v < 0 ? "−" : ""}${Math.abs(v).toFixed(d)}%`;
};
export const tone = (x: unknown) => (n(x) > 0 ? "text-up" : n(x) < 0 ? "text-down" : "text-dim");
export const clsx = (...a: (string | false | null | undefined)[]) => a.filter(Boolean).join(" ");
