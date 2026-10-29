/**
 * Browser-side operational transformation (WBS 5.1 / 7.1).
 *
 * A port of server/app/realtime/ot.py, using the same wire format: a flat
 * array where a positive number is a retain, a negative number is a delete,
 * and a string is an insert. Both ends must agree on transform() exactly, or
 * the two copies of the document drift apart -- so this file mirrors the
 * Python one case for case rather than reimplementing the idea.
 */

export const isRetain = (op) => typeof op === "number" && op > 0;
export const isDelete = (op) => typeof op === "number" && op < 0;
export const isInsert = (op) => typeof op === "string";

export class TextOperation {
  constructor(ops) {
    this.ops = [];
    this.baseLength = 0;
    this.targetLength = 0;
    for (const op of ops || []) {
      if (isRetain(op)) this.retain(op);
      else if (isDelete(op)) this.delete(-op);
      else if (isInsert(op)) this.insert(op);
    }
  }

  retain(n) {
    if (n === 0) return this;
    this.baseLength += n;
    this.targetLength += n;
    const last = this.ops[this.ops.length - 1];
    if (isRetain(last)) this.ops[this.ops.length - 1] += n;
    else this.ops.push(n);
    return this;
  }

  insert(text) {
    if (text === "") return this;
    this.targetLength += text.length;
    const ops = this.ops;
    const last = ops[ops.length - 1];
    if (isInsert(last)) {
      ops[ops.length - 1] += text;
    } else if (isDelete(last)) {
      // Keep inserts ahead of deletes so equivalent operations look equal.
      if (ops.length > 1 && isInsert(ops[ops.length - 2])) ops[ops.length - 2] += text;
      else ops.splice(ops.length - 1, 0, text);
    } else {
      ops.push(text);
    }
    return this;
  }

  delete(n) {
    if (n === 0) return this;
    this.baseLength += n;
    const last = this.ops[this.ops.length - 1];
    if (isDelete(last)) this.ops[this.ops.length - 1] -= n;
    else this.ops.push(-n);
    return this;
  }

  isNoop() {
    return this.ops.length === 0 || (this.ops.length === 1 && isRetain(this.ops[0]));
  }

  apply(doc) {
    if (doc.length !== this.baseLength) {
      throw new Error(
        `operation expects a document of length ${this.baseLength}, got ${doc.length}`
      );
    }
    const parts = [];
    let cursor = 0;
    for (const op of this.ops) {
      if (isRetain(op)) {
        parts.push(doc.slice(cursor, cursor + op));
        cursor += op;
      } else if (isInsert(op)) {
        parts.push(op);
      } else {
        cursor -= op;
      }
    }
    return parts.join("");
  }

  invert(doc) {
    const inverse = new TextOperation();
    let cursor = 0;
    for (const op of this.ops) {
      if (isRetain(op)) {
        inverse.retain(op);
        cursor += op;
      } else if (isInsert(op)) {
        inverse.delete(op.length);
      } else {
        inverse.insert(doc.slice(cursor, cursor - op));
        cursor -= op;
      }
    }
    return inverse;
  }

  transformCursor(position) {
    let index = position;
    let cursor = 0;
    for (const op of this.ops) {
      if (cursor > position) break;
      if (isRetain(op)) {
        cursor += op;
      } else if (isInsert(op)) {
        index += op.length;
      } else {
        const removed = -op;
        index -= Math.min(removed, Math.max(0, position - cursor));
        cursor += removed;
      }
    }
    return Math.max(0, index);
  }

  toJSON() {
    return this.ops.slice();
  }

  static fromJSON(ops) {
    return new TextOperation(ops);
  }

  /** The minimal single-splice operation turning `oldText` into `newText`. */
  static fromDiff(oldText, newText) {
    let prefix = 0;
    const limit = Math.min(oldText.length, newText.length);
    while (prefix < limit && oldText[prefix] === newText[prefix]) prefix += 1;
    let suffix = 0;
    while (
      suffix < limit - prefix &&
      oldText[oldText.length - 1 - suffix] === newText[newText.length - 1 - suffix]
    ) {
      suffix += 1;
    }
    // Never split a surrogate pair: half an emoji is not a character the
    // server can count, and cannot travel as well-formed JSON.
    const isHigh = (code) => code >= 0xd800 && code <= 0xdbff;
    const isLow = (code) => code >= 0xdc00 && code <= 0xdfff;
    if (prefix > 0 && isHigh(oldText.charCodeAt(prefix - 1))) prefix -= 1;
    if (suffix > 0 && isLow(oldText.charCodeAt(oldText.length - suffix))) suffix -= 1;
    return new TextOperation()
      .retain(prefix)
      .delete(oldText.length - prefix - suffix)
      .insert(newText.slice(prefix, newText.length - suffix))
      .retain(suffix);
  }

  static compose(a, b) {
    if (a.targetLength !== b.baseLength) {
      throw new Error("cannot compose: lengths do not line up");
    }
    const out = new TextOperation();
    const it1 = cursorOver(a.ops);
    const it2 = cursorOver(b.ops);
    let op1 = it1();
    let op2 = it2();
    for (;;) {
      if (op1 === undefined && op2 === undefined) break;
      if (isDelete(op1)) {
        out.delete(-op1);
        op1 = it1();
        continue;
      }
      if (isInsert(op2)) {
        out.insert(op2);
        op2 = it2();
        continue;
      }
      if (op1 === undefined || op2 === undefined) {
        throw new Error("cannot compose: operations do not line up");
      }
      if (isRetain(op1) && isRetain(op2)) {
        if (op1 > op2) {
          out.retain(op2);
          op1 -= op2;
          op2 = it2();
        } else if (op1 === op2) {
          out.retain(op1);
          op1 = it1();
          op2 = it2();
        } else {
          out.retain(op1);
          op2 -= op1;
          op1 = it1();
        }
      } else if (isInsert(op1) && isDelete(op2)) {
        if (op1.length > -op2) {
          op1 = op1.slice(-op2);
          op2 = it2();
        } else if (op1.length === -op2) {
          op1 = it1();
          op2 = it2();
        } else {
          op2 += op1.length;
          op1 = it1();
        }
      } else if (isInsert(op1) && isRetain(op2)) {
        if (op1.length > op2) {
          out.insert(op1.slice(0, op2));
          op1 = op1.slice(op2);
          op2 = it2();
        } else if (op1.length === op2) {
          out.insert(op1);
          op1 = it1();
          op2 = it2();
        } else {
          out.insert(op1);
          op2 -= op1.length;
          op1 = it1();
        }
      } else {
        // retain against delete
        if (op1 > -op2) {
          out.delete(-op2);
          op1 += op2;
          op2 = it2();
        } else if (op1 === -op2) {
          out.delete(op1);
          op1 = it1();
          op2 = it2();
        } else {
          out.delete(op1);
          op2 += op1;
          op1 = it1();
        }
      }
    }
    return out;
  }

  /** Returns [aPrime, bPrime] for two operations written against one document. */
  static transform(a, b) {
    if (a.baseLength !== b.baseLength) {
      throw new Error("cannot transform operations from different document states");
    }
    const aPrime = new TextOperation();
    const bPrime = new TextOperation();
    const it1 = cursorOver(a.ops);
    const it2 = cursorOver(b.ops);
    let op1 = it1();
    let op2 = it2();
    for (;;) {
      if (op1 === undefined && op2 === undefined) break;
      if (isInsert(op1)) {
        aPrime.insert(op1);
        bPrime.retain(op1.length);
        op1 = it1();
        continue;
      }
      if (isInsert(op2)) {
        aPrime.retain(op2.length);
        bPrime.insert(op2);
        op2 = it2();
        continue;
      }
      if (op1 === undefined || op2 === undefined) {
        throw new Error("cannot transform: operations do not line up");
      }
      let shared;
      if (isRetain(op1) && isRetain(op2)) {
        if (op1 > op2) {
          shared = op2;
          op1 -= op2;
          op2 = it2();
        } else if (op1 === op2) {
          shared = op1;
          op1 = it1();
          op2 = it2();
        } else {
          shared = op1;
          op2 -= op1;
          op1 = it1();
        }
        aPrime.retain(shared);
        bPrime.retain(shared);
      } else if (isDelete(op1) && isDelete(op2)) {
        // The same characters were removed twice; neither side repeats it.
        if (-op1 > -op2) {
          op1 -= op2;
          op2 = it2();
        } else if (op1 === op2) {
          op1 = it1();
          op2 = it2();
        } else {
          op2 -= op1;
          op1 = it1();
        }
      } else if (isDelete(op1) && isRetain(op2)) {
        if (-op1 > op2) {
          shared = op2;
          op1 += op2;
          op2 = it2();
        } else if (-op1 === op2) {
          shared = op2;
          op1 = it1();
          op2 = it2();
        } else {
          shared = -op1;
          op2 += op1;
          op1 = it1();
        }
        aPrime.delete(shared);
      } else {
        if (op1 > -op2) {
          shared = -op2;
          op1 += op2;
          op2 = it2();
        } else if (op1 === -op2) {
          shared = op1;
          op1 = it1();
          op2 = it2();
        } else {
          shared = op1;
          op2 += op1;
          op1 = it1();
        }
        bPrime.delete(shared);
      }
    }
    return [aPrime, bPrime];
  }
}

function cursorOver(ops) {
  let i = 0;
  return () => (i < ops.length ? ops[i++] : undefined);
}

/**
 * The OT client state machine.
 *
 * Exactly one operation may be in flight at a time. Anything typed while the
 * server has yet to acknowledge it is composed into a buffer and sent as a
 * single operation once the acknowledgement arrives. The three states below
 * are the standard ones; keeping them explicit is what makes it possible to
 * reason about a client that is mid-edit when a remote change lands.
 */
export class OTClient {
  constructor(version, send) {
    this.version = version;
    this.send = send;
    this.outstanding = null; // sent, not yet acknowledged
    this.buffer = null; // typed since, not yet sent
  }

  get state() {
    if (!this.outstanding) return "synchronised";
    return this.buffer ? "awaiting-with-buffer" : "awaiting-confirm";
  }

  /** The local user typed. Returns nothing; the operation is queued or sent. */
  applyClient(operation) {
    if (!this.outstanding) {
      this.outstanding = operation;
      this.send(operation, this.version);
    } else if (!this.buffer) {
      this.buffer = operation;
    } else {
      this.buffer = TextOperation.compose(this.buffer, operation);
    }
  }

  /** The server accepted our outstanding operation. */
  serverAck() {
    this.version += 1;
    if (this.buffer) {
      this.outstanding = this.buffer;
      this.buffer = null;
      this.send(this.outstanding, this.version);
    } else {
      this.outstanding = null;
    }
  }

  /**
   * A remote operation arrived. Returns the operation to apply to the local
   * document, rebased over anything of ours that is still in flight.
   */
  applyServer(operation) {
    this.version += 1;
    let incoming = operation;
    if (this.outstanding) {
      [this.outstanding, incoming] = TextOperation.transform(this.outstanding, incoming);
      if (this.buffer) {
        [this.buffer, incoming] = TextOperation.transform(this.buffer, incoming);
      }
    }
    return incoming;
  }
}
