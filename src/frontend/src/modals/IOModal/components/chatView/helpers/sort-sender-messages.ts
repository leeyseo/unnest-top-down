import type { ChatMessageType } from "../../../../../types/chat";
import { parseApiTimestamp } from "../../../../../utils/dateTime";

// Cache for parsed timestamps to improve performance during sorting
const timestampCache = new WeakMap<ChatMessageType, number>();

/**
 * Sorts chat messages by timestamp with proper handling of identical timestamps.
 *
 * Primary sort: By timestamp (chronological order)
 * Secondary sort: When timestamps are identical, User messages (isSend=true) come before AI/Machine messages (isSend=false)
 *
 * This ensures proper conversation flow even when backend generates identical timestamps
 * due to streaming, load balancing, or database precision limitations.
 *
 * @param a - First chat message to compare
 * @param b - Second chat message to compare
 * @returns Sort comparison result (-1, 0, 1)
 */
const sortSenderMessages = (a: ChatMessageType, b: ChatMessageType): number => {
  // Use WeakMap cache to avoid repeated Date parsing for same message objects
  let timeA = timestampCache.get(a);
  if (timeA === undefined) {
    timeA = parseApiTimestamp(a.timestamp)?.getTime() ?? Number.NaN;
    timestampCache.set(a, timeA);
  }

  let timeB = timestampCache.get(b);
  if (timeB === undefined) {
    timeB = parseApiTimestamp(b.timestamp)?.getTime() ?? Number.NaN;
    timestampCache.set(b, timeB);
  }

  if (Number.isNaN(timeA)) {
    if (!Number.isNaN(timeB)) return 1;
  } else if (Number.isNaN(timeB)) {
    return -1;
  }

  // Primary sort: by timestamp
  if (!Number.isNaN(timeA) && !Number.isNaN(timeB) && timeA !== timeB) {
    return timeA - timeB;
  }

  // Secondary sort: if timestamps are identical, User messages come before AI/Machine
  // This ensures proper chronological order when backend generates identical timestamps
  if (a.isSend && !b.isSend) {
    return -1; // User message (isSend=true) comes first
  }
  if (!a.isSend && b.isSend) {
    return 1; // User message (isSend=true) comes first
  }

  return 0; // Keep original order for same sender types
};

export default sortSenderMessages;
