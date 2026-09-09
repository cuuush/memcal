import { registerViews, show } from "./core.js";
import { loadGate } from "./gate.js";
import { loadChats } from "./chats.js";
import { loadDream } from "./dream.js";
import { loadSenders } from "./senders.js";
import { loadMemory } from "./memory.js";
import { loadWiki } from "./wiki.js";
import { loadRuns } from "./runs.js";

registerViews({gate: loadGate, chats: loadChats, dream: loadDream,
               senders: loadSenders, memory: loadMemory, wiki: loadWiki,
               runs: loadRuns});

show(location.hash.slice(1));
