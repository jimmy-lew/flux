-- skip_intro.lua
-- Automatically skips chapters whose titles match intro/OP/opening patterns.
-- Loaded by flux when -i is passed. Also binds 'I' to toggle on/off at runtime.

local enabled = true
local skipped  = {}   -- track which chapters we've already skipped this file

-- Patterns matched case-insensitively against chapter titles
local SKIP_PATTERNS = {
  "^op$", "^opening$",
  "^intro$", "^introduction$",
  "op %d", "opening %d",
  "intro %d",
  "^credits$", "^opening credits$",
}

local function matches_skip(title)
  if not title then return false end
  local t = title:lower():gsub("^%s+", ""):gsub("%s+$", "")
  for _, pat in ipairs(SKIP_PATTERNS) do
    if t:match(pat) then return true end
  end
  return false
end

local function skip_if_intro()
  if not enabled then return end

  local chapters = mp.get_property_native("chapter-list")
  local chapter  = mp.get_property_number("chapter")
  if not chapters or chapter == nil then return end

  local idx = chapter + 1  -- Lua 1-based
  local entry = chapters[idx]
  if not entry then return end

  local title = entry.title or ""
  if matches_skip(title) and not skipped[idx] then
    -- find the end of this chapter (start of next) or end of file
    local next_entry = chapters[idx + 1]
    if next_entry then
      skipped[idx] = true
      mp.osd_message(string.format("⏭  Skipping: %s", title), 2)
      mp.set_property("time-pos", next_entry.time)
    end
  end
end

-- Re-check on every chapter change
mp.observe_property("chapter", "number", function(_, _)
  skip_if_intro()
end)

-- Reset per-file skip history when a new file loads
mp.register_event("file-loaded", function()
  skipped = {}
  skip_if_intro()
end)

-- Runtime toggle with 'I'
mp.add_key_binding("I", "toggle-skip-intro", function()
  enabled = not enabled
  mp.osd_message(string.format("Skip intro: %s", enabled and "ON" or "OFF"), 2)
end)

mp.osd_message("Skip intro: ON  (press I to toggle)", 2)
