"use strict";
const ActivityLogic = {
  category(row) {
    if (["watched","favorite","playback"].includes(row.category)) return row.category;
    if (["marked_played","marked_unplayed","completed"].includes(row.event)) return "watched";
    if (row.event === "favorited") return "favorite";
    return "playback";
  },
  matches(row, state="all", type="important") {
    const category = this.category(row);
    const matchesType = type === "all" || (type === "important" ? category !== "playback" : category === type);
    const matchesState = state === "all" || (state === "pending" && ["queued","retrying"].includes(row.state)) || (state === "failed" && ["failed","blocked"].includes(row.state)) || (state === "sent" && row.state === "sent");
    return matchesType && matchesState;
  }
};
if (typeof module !== "undefined") module.exports = ActivityLogic;
