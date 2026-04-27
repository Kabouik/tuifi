# tuifi

> [!WARNING]
> TIDAL has been blocking HiFi API accounts en masse starting from around now. Accounts (and therefore tokens) are not meant to be shared, as per TIDAL's terms of service for individual accounts. Sharing an instance is very likely to raise red flags and get the associated account blocked.

1.  [Features](#features)
2.  [Usage](#usage)
3.  [Requirements](#requirements)
4.  [Installation](#installation)
    1.  [Windows peculiarities](#windows-peculiarities)
5.  [Configuration](#configuration)
6.  [Development note](#development-note)
7.  [Disclaimer](#disclaimer)
    1.  [Content extraction](#content-extraction)
    2.  [DMCA and copyright infringements](#dmca-and-copyright-infringements)
8.  [License](#license)
9.  [Mirrors](#mirrors)

A feature-rich TUI music player built on top of TDAL API and [HiFi API](https://github.com/binimum/hifi-api): browse, search, stream, mix, fetch locally, find artists, organize and manage lossless music from your comfy terminal.

While [HiFi API instances with unrestricted access](https://github.com/monochrome-music/monochrome/blob/main/INSTANCES.md#official--community-apis) used to exist, this may be considered as music piracy in many countries and does not give artists the love they deserve. This project is intended for TIDAL subscribers who also self-host their own [HiFi](https://github.com/binimum/hifi-api) instance for convenience, and favor a keyboard-driven terminal workflow over a web player. TIDAL offers a free trial on their main plan if you want to give it a go for free.

[![Click to play the demo](demo/ss20260312-191917.png)](https://github.com/Kabouik/tuifi/raw/refs/heads/main/demo/tuifi-demo.mp4)
[![Click to play the demo](demo/ss20260312-191954.png)](https://github.com/Kabouik/tuifi/raw/refs/heads/main/demo/tuifi-demo.mp4)
[![Click to play the demo](demo/ss20260312-192026.png)](https://github.com/Kabouik/tuifi/raw/refs/heads/main/demo/tuifi-demo.mp4)
[![Click to play the demo](demo/ss20260312-192044.png)](https://github.com/Kabouik/tuifi/raw/refs/heads/main/demo/tuifi-demo.mp4)

*Click any screenshot to play the demo video. Another video from an older version is available [here](demo/tuifi-demo_old.mp4), showing some other features.*

# Features

## Playback & queue

- Playback control (play, pause, (auto)resume, seek, volume, repeat, shuffle)
- Autoextend queue with mix or recommendations (infinite queue)
- Queue management with reordering and priority flags
- Playback history
- Lyrics display
- Cover art (requires a compatible terminal emulator)

## Library management & discovery

- Search, browse artists/albums, recommendations, mixes
- Find similar artists
- Playlists (create, delete, add/remove tracks)
- Like tracks, albums, artists, mixes and playlists
- Accountless library: playlists, history, liked songs and queue are kept in standard json files that some web services can import
  
## Downloads

- If you are choosing a TUI, then there likely is no official TIDAL application packaged for your platform or distribution, and you have no way to use the service offline (_e.g._, during flights), so `tuifi` can prefetch individual or multiple tracks (_e.g._, marked, albums, playlists, artists) for offline playback in other music players

> [!WARNING]
> Beware that offline fetching is intended for ephemeral listening since your TIDAL subscription does not grant you permanent access to the catalog, only to their service while you are a subscriber, and owning a physical copy of the media is likely required even with a valid TIDAL subscription to legitimately keep a local file. Consequently, `tuifi` defaults to the `/tmp` folder due to its ephemeral nature to discourage permanent copies, by lack of a proper method to date to play TIDAL DRM material directly from the CLI and in a multiplatform way (where ephemeral storage would otherwise no longer be advised).

## Interface & interaction

- Contextual menus on tracks, albums & artists
- Keyboard-oriented control
- Customizable (colors, optional TSV mode, show/hide metadata fields, file hierarchy for local files, autoextend buffer, autoresume playback on launch, _etc._)

# Usage

    Usage: ./tuifi [options]

    Options:
      --api URL, -a URL        TIDAL HiFi API base URL (can also be set in settings.jsonc)
      --clear-covers           Delete cached cover art images and exit
      --keep SOURCES           With --clear-covers: keep covers for tracks in SOURCES
      --fetch-covers SOURCES   Pre-download covers for tracks in SOURCES and exit
                               SOURCES: comma-separated list of liked, history, queue, playlists
      --verbose, -v            Write debug log to debug.log in the config directory
      --version, -V            Show version

    Press ? in tuifi for keybindings

# Requirements

- Python 3.7+ (3.13 on Windows)
- [mpv](https://mpv.io)
- [ffmpeg](https://ffmpeg.org/download.html)
- (Optional for cover art rendering) [chafa](https://github.com/hpjansson/chafa/) and/or [ueberzugpp](https://github.com/jstkdng/ueberzugpp) and a terminal emulator [compatible](https://www.arewesixelyet.com/) with Sixel or Kitty graphics
- (Optional for the audio spectrum visualiser) [cava](https://github.com/karlstav/cava)

# Installation

Install the dependencies with your favourite package manager. `tuifi` comes with a wrapper script (named `tuifi` too) at the root of the project, which will handle calling the different Python files. Threfore, it requires no system installation and this repository can just be cloned before executing the wrapper script:

    git clone https://git.sr.ht/~matf/tuifi && cd tuifi
    ./tuifi

You can also create a symbolic link between the script and a directory in your `$PATH` so that `tuifi` can be executed from any directory, *e.g.*:

    mkdir -p ~/.local/bin
    ln -s /path/to/tuifi/tuifi ~/.local/bin/tuifi

## Windows peculiarities

NOTE: Windows support may be partly broken in the latest commit, I will investigate it but have no machine to test it, hence why it is not done already.

The `ncurses` library does not exist officially for Windows, but `windows-curses` can be used. It is available via `pip` up to Python 3.13. The easiest way to install a specific Python version on Windows as well as the other dependency, `mpv`, is probably using [Chocolatey](https://chocolatey.org/install):

    choco install python313 mpv git -y # In an admin PowerShell
    python3.13.exe -m pip install windows-curses

Then make a shortcut that uses Python 3.13 specifically, or the following command:

    python3.13.exe /path/to/tuifi

# Configuration

While `tuifi` should be compatible with any HiFi API instance, those that are made public violate TIDAL's terms of service and may therefore be very shortlived, for a reason. Consequently, the program is delivered with no default instance set, and users should set their preferred instance either using the `--api` runtime flag or by editing `settings.jsonc`. Users choosing to use a HiFi instance shared by someone with no legitimate TIDAL subscription do so at their own risk. TIDAL does offer a one-month trial if you want to give it a go before deciding on your subscription.

Settings are stored in `settings.jsonc` and automatically updated upon using toggles within the TUI. On first run, `tuifi` will prompt before creating the config directory.

Configuration file directory per platform:

<table border="2" cellspacing="0" cellpadding="6" rules="groups" frame="hsides">
<colgroup>
<col class="org-left" />
<col class="org-left" />
</colgroup>
<thead>
<tr>
<th scope="col" class="org-left">Platform</th>
<th scope="col" class="org-left">Path</th>
</tr>
</thead>
<tbody>
<tr>
<td class="org-left">Linux</td>
<td class="org-left"><code>~/.config/tuifi</code> (or <code>$XDG_CONFIG_HOME/tuifi</code>)</td>
</tr>
<tr>
<td class="org-left">Termux</td>
<td class="org-left"><code>/data/data/com.termux/files/home/.config/tuifi</code></td>
</tr>
<tr>
<td class="org-left">macOS</td>
<td class="org-left"><code>~/Library/Application Support/tuifi</code></td>
</tr>
<tr>
<td class="org-left">Windows</td>
<td class="org-left"><code>%APPDATA%\tuifi</code></td>
</tr>
</tbody>
</table>

`settings.jsonc` can be edited to change UI options, colours, metadata field widths, autoextend buffer size, file structure conventions, TIDAL HiFi API URL/IP, _etc._ Check the file for options not documented in this README.

Other state files stored in the same directory:

- `queue.json` keeps your current play queue and persists among `tuifi` executions,
- `liked.json` stores liked tracks, albums, artists, mixes and playlists,
- `playlists.json` stores playlists,
- `history.json` keeps the playback history.

Cover art images are cached separately in the platform cache directory (`~/.cache/tuifi/cover_cache` on Linux, `~/Library/Caches/tuifi/cover_cache` on macOS, `%LOCALAPPDATA%\tuifi\cover_cache` on Windows). Use `--clear-covers` to delete the cache, optionally preserving covers for specific sources with `--keep`. Use `--fetch-covers` to pre-download covers for tracks in your library.

`liked.json` and `playlists.json` are fully compatible with Monochrome (e.g., <https://monochrome.tf>) and can be imported there.

# Development note

This program was developed with significant AI assistance. I take no particular pride in that, or the resulting code, but it is fair to be honest about it. It was a week-end project and I wanted something usable quickly to enjoy TIDAL on my distribution (which ahs no official native TIDAL package because they did not deem it relevant) rather than something to be proud of architecturally.

# Disclaimer

## Content extraction

Any content accessed by this project is hosted by external non-affiliated sources, and everything served through `tuifi` is accessible _via_ the TIDAL API and Hi-Fi API, which both require a valid subscription to TIDAL. A web browser makes hundreds of requests to get everything made available by a site, this project goes on to make more targeted requests associated with only getting the content relevant to its purpose without the associated web UI clutter. If this project accesses your content, or content provided by your service in a way that is considered unintended, the code is public and may help taking the necessary measures to counter the means to access it in the first place.

## DMCA and copyright infringements

This project is to be used at the user's own risk, based on their government and laws. No audio files or direct links to audio files are stored in this repository. The TUI merely interfaces with sources and APIs that exist independently and are available on the open Internet to anyone with a valid TIDAL account. This project has no control over the content it finds at any point in time, and no control over the content served by the source service, it just uses documented external APIs, official or third-party, to fetch targeted information and content otherwise available with a web browser, to make it work in a more performant and minimal terminal interface.

Hence, any copyright infringements or DMCA claims in this project's regards are to be forwarded to the associated content provider or APIs by the associated notifier of any such claims. Just like a web browser or search engine, this console program is not made to infringe copyrights and is designed to discourage such wrongdoings (by _e.g._ defaulting to ephemeral offline storage for copyrighted material, as opposed to permanent cache for covers), and users are responsible with how they use the tool. The content that can be queried to TIDAL using the third-party API is not a valid reason to send a DMCA notice to this git server holders or the maintainers of this repository, and is to be routed to the the API repository directly. If any source accessed using the present program infringes on your rights as a copyright holder, they may be removed by contacting the service that published them online, is actually hosting them, or is routing them (not this repository, nor its maintainers).

# License

[GNU Affero General Public License v3.0](https://www.gnu.org/licenses/agpl-3.0.html)

# Mirrors

<https://git.sr.ht/~matf/tuifi>, <https://codeberg.org/kabouik/tuifi>, <https://github.com/kabouik/tuifi>
