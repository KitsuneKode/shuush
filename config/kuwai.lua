-- First-party kuwai rice integration for Shuush.
-- Add this line to ~/.config/hypr/user.lua (before kuwai loads modules/voxtype.lua)
-- or require this file directly:
--
--   K.voxtype_scripts = K.home .. "/.local/share/shuush/libexec/"
--
-- kuwai's built-in hypr/kuwai/voxtype.lua automatically binds:
--   SUPER + CTRL + D          Dictate toggle (context-aware profile)
--   F9 (hold / tap <300ms)    Push-to-talk / hands-free
--   SUPER + CTRL + E          Voice command on highlighted selection
--   SUPER + SHIFT + D / Esc   Cancel dictation (double-tap Esc guard)
--   SUPER + CTRL + SHIFT + D  Teach dictation fix from clipboard
--   SUPER + ALT + D           Open Shuush Studio (floating 1140x720)
--
-- Notifications automatically route through kuwai-notify, and when
-- kuwai-shell.service hosts the island OSD (VOXTYPE_OSD_HOST), Shuush
-- leaves OSD hosting to kuwai without conflict.

local K = require("kuwai.core")
K.voxtype_scripts = K.home .. "/.local/share/shuush/libexec/"
