/**
 * Offsets on the wire are Unicode code points; offsets in the browser are
 * UTF-16 code units.
 *
 * The server's OT engine is Python, where `len("💡") == 1`; JavaScript says
 * `"💡".length === 2`. Both engines are correct in their own units, but an
 * operation that retains 10 units in the browser retains fewer characters on
 * the server as soon as the text holds an emoji -- and from then on the two
 * copies of the document disagree about every position after it.
 *
 * So the client converts at the socket: every operation it sends is measured
 * in code points against the server's text, and every operation it receives
 * is converted back. The text an operation applies to is always known -- it
 * is the server document at the version the operation names -- which the
 * client tracks as `serverText`.
 */
import { TextOperation } from "./ot.js";

const isHigh = (code) => code >= 0xd800 && code <= 0xdbff;
const isLow = (code) => code >= 0xdc00 && code <= 0xdfff;

function hasSurrogates(text) {
  for (let i = 0; i < text.length; i += 1) {
    const code = text.charCodeAt(i);
    if (code >= 0xd800 && code <= 0xdfff) return true;
  }
  return false;
}

/** Code points in text[from, to); a pair counts once, at its high half. */
function codePointsBetween(text, from, to) {
  let count = 0;
  for (let i = from; i < to; i += 1) {
    if (isLow(text.charCodeAt(i)) && i > 0 && isHigh(text.charCodeAt(i - 1))) continue;
    count += 1;
  }
  return count;
}

/** UTF-16 length of `count` code points starting at `from`. */
function unitsFor(text, from, count) {
  let i = from;
  let left = count;
  while (left > 0 && i < text.length) {
    i += isHigh(text.charCodeAt(i)) && isLow(text.charCodeAt(i + 1)) ? 2 : 1;
    left -= 1;
  }
  // Running past the end means the operation does not fit this text; keep
  // the excess so that apply() reports the mismatch instead of hiding it.
  return i - from + left;
}

/** A browser operation (UTF-16 units) as the server measures it. */
export function toServerOperation(operation, doc) {
  if (!hasSurrogates(doc)) return operation;
  const converted = new TextOperation();
  let index = 0;
  for (const op of operation.ops) {
    if (typeof op === "string") {
      converted.insert(op);
      continue;
    }
    const length = Math.abs(op);
    const points = codePointsBetween(doc, index, index + length);
    if (op > 0) converted.retain(points);
    else converted.delete(points);
    index += length;
  }
  return converted;
}

/** A server operation (code points) as the browser measures it. */
export function fromServerOperation(operation, doc) {
  if (!hasSurrogates(doc)) return operation;
  const converted = new TextOperation();
  let index = 0;
  for (const op of operation.ops) {
    if (typeof op === "string") {
      converted.insert(op);
      continue;
    }
    const length = unitsFor(doc, index, Math.abs(op));
    if (op > 0) converted.retain(length);
    else converted.delete(length);
    index += length;
  }
  return converted;
}

export const toServerOffset = (doc, offset) =>
  hasSurrogates(doc) ? codePointsBetween(doc, 0, Math.min(offset, doc.length)) : offset;

export const fromServerOffset = (doc, offset) =>
  hasSurrogates(doc) ? Math.min(doc.length, unitsFor(doc, 0, offset)) : offset;
