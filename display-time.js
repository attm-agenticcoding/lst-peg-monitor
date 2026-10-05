/* Display-only US Eastern timestamps; source timestamps and arithmetic stay UTC.
 * Inputs: ISO timestamp string, Date, or epoch milliseconds (not Unix seconds).
 * Missing/invalid inputs render as an em dash. All output includes EDT or EST.
 */
(function (root, factory) {
  if (typeof module === "object" && module.exports) module.exports = factory();
  else root.DisplayTime = factory();
})(typeof self !== "undefined" ? self : this, function () {
  "use strict";
  const TIME_ZONE = "America/New_York";
  const formatter = new Intl.DateTimeFormat("en-US", {
    timeZone: TIME_ZONE, calendar: "gregory", numberingSystem: "latn",
    year: "numeric", month: "2-digit", day: "2-digit",
    hour: "2-digit", minute: "2-digit", second: "2-digit",
    hourCycle: "h23", timeZoneName: "short"
  });
  function parts(value) {
    if (value == null || value === "" || !["string", "number"].includes(typeof value)
      && Object.prototype.toString.call(value) !== "[object Date]") return null;
    const date = new Date(value);
    if (!Number.isFinite(date.getTime())) return null;
    return Object.fromEntries(formatter.formatToParts(date).map(({ type, value }) => [type, value]));
  }
  function formatTimestamp(value) {
    const p = parts(value);
    return p ? `${p.year}-${p.month}-${p.day} ${p.hour}:${p.minute}:${p.second} ${p.timeZoneName}` : "—";
  }
  function formatDate(value) {
    const p = parts(value);
    return p ? `${p.year}/${p.month}/${p.day} ${p.timeZoneName}` : "—";
  }
  function formatClock(value) {
    const p = parts(value);
    return p ? `${p.hour}:${p.minute}:${p.second} ${p.timeZoneName}` : "—";
  }
  function formatShort(value) {
    const p = parts(value);
    return p ? `${p.month}/${p.day} ${p.hour}:${p.minute} ${p.timeZoneName}` : "—";
  }
  return { TIME_ZONE, formatTimestamp, formatDate, formatClock, formatShort };
});
