import logging
import os
import re
import tempfile
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime

from podgen import Podcast, Episode, Media
from dateutil import parser
from datetime import timedelta

from common.helpers import (
    init,
    get_last_feed,
    get_podcasts_config,
    write_feeds_file,
    get_version,
)
from common.psapi import (
    get_podcast_metadata,
    get_episode_manifest,
    get_podcast_episodes,
    get_all_podcast_episodes,
    get_all_podcast_episodes_all_seasons,
)


podgen_agent = f"nrk-pod-feeder v{get_version()} (with help from python-podgen)"
podcasts_cfg_file = "podcasts.json"
filter_teasers = True
web_url = "https://sindrel.github.io/nrk-pod-feeds"

ARCHIVE_NS = "https://sindrel.github.io/nrk-pod-feeds/archive"
ET.register_namespace("nrk", ARCHIVE_NS)


def feed_has_archive_marker(existing_feed):
    if existing_feed is None:
        return False

    marker = existing_feed.find(
        f".//{{{ARCHIVE_NS}}}archiveInitialized"
    )

    return marker is not None and marker.text == "true"


def get_podcast(podcast_id, season, feeds_dir, ep_count=10):
    existing_feed = get_last_feed(feeds_dir, podcast_id)

    last_feed_update = parser.parse("1970-01-01 00:00:01+00:00")

    if existing_feed:
        for channel in existing_feed.findall("channel"):
            last_build_date = channel.find("lastBuildDate")

            if last_build_date is not None and last_build_date.text:
                last_feed_update = parser.parse(last_build_date.text)
                logging.debug(
                    f"Feed was last built {last_feed_update}"
                )

    metadata = get_podcast_metadata(podcast_id)

    if not metadata:
        return None

    original_title = metadata["series"]["titles"]["title"]
    image = f"{metadata['series']['squareImage'][4]['url']}.jpg"
    website = metadata["_links"]["share"]["href"]

    logging.debug(f"  Title: {original_title}")
    logging.debug(f"  Image: {image}")

    p = Podcast(
        generator=podgen_agent,
        website=web_url,
        image=image,
        withhold_from_itunes=True,
        explicit=False,
        language="no",
    )

    if season == "LATEST_SEASON":
        season = metadata["_links"]["seasons"][0]["name"]

    # ---------------------------------------------------------
    # ARCHIVE MODE
    #
    # episodes == 0 means:
    #
    #   1. No archive yet -> fetch everything.
    #   2. Archive already initialized -> fetch only newest 10.
    # ---------------------------------------------------------

    archive_mode = ep_count == 0
    archive_initialized = feed_has_archive_marker(existing_feed)

    if archive_mode and not archive_initialized:
        logging.info(
            "  Archive not initialized - fetching complete archive"
        )

        if season == "ALL":
            episodes = get_all_podcast_episodes_all_seasons(
                podcast_id,
                metadata,
            )
        else:
            episodes = get_all_podcast_episodes(
                podcast_id,
                season,
            )

else:
    # Normal mode - fetch newest episodes
    episodes = get_podcast_episodes(
        podcast_id,
        season,
    )

    episodes = episodes[:ep_count]

if not episodes:
    return None
    # ---------------------------------------------------------
    # NORMAL UPDATE MODE
    #
    # For an already initialized feed, only episodes newer
    # than the previous feed build are interesting.
    # ---------------------------------------------------------

    if not archive_mode or archive_initialized:
        new_episode = False

        for episode in episodes:
            episode_title = episode["titles"]["title"]
            episode_date = episode["date"]

            if parser.parse(episode_date) >= last_feed_update:
                logging.info(
                    f"  Found new episode {episode_title} "
                    f"from {episode_date}"
                )
                new_episode = True

        if not new_episode:
            logging.info(
                "  No new episodes found since feed was last updated"
            )
            return None

    else:
        logging.info(
            f"  Full archive mode: rebuilding feed with "
            f"{len(episodes)} episodes"
        )

    # ---------------------------------------------------------
    # BUILD PODGEN FEED WITH NEW EPISODES
    # ---------------------------------------------------------

    ep_i = 0

    for episode in episodes:
        logging.info(f"Episode #{ep_i}:")

        episode_id = episode["episodeId"]
        episode_title = episode["titles"]["title"]
        episode_subtitle = episode["titles"]["subtitle"]
        episode_image = f"{episode['squareImage'][4]['url']}.jpg"
        duration = episode["durationInSeconds"]
        date = episode["date"]

        manifest = get_episode_manifest(
            podcast_id,
            episode_id,
        )

        if not manifest:
            logging.info(
                f"  No manifest found for: {episode_title}"
            )
            continue

        logging.info(
            f"  *** MANIFEST FOUND FOR: {episode_title} ***"
        )

        audio_mime = manifest["playable"]["assets"][0]["mimeType"]
        audio_url = manifest["playable"]["assets"][0]["url"]

        logging.info(f"  Episode title: {episode_title}")
        logging.info(f"  Episode duration: {duration}")
        logging.info(f"  Episode date: {date}")
        logging.info(f"  Audio file URL: {audio_url}")
        logging.info(f"  Episode image URL: {episode_image}")
        logging.info(f"  *** AUDIO TYPE: {audio_mime} ***")

        if audio_mime != "audio/mp3":
            logging.info(
                f"  Unrecognized audio MIME type ({audio_mime})"
            )
            continue

        if filter_teasers and episode_title.startswith(
            "Neste episode: "
        ):
            logging.info("  Skipping teaser")
            continue

        p.episodes += [
            Episode(
                title=episode_title,
                media=Media(
                    audio_url,
                    0,
                    duration=timedelta(seconds=duration),
                ),
                summary=episode_subtitle,
                publication_date=parser.parse(date),
                image=episode_image,
            ),
        ]

        ep_i += 1

    if ep_i == 0:
        logging.info(
            "  No playable episodes found"
        )
        return None

    title = f"De {ep_i} siste fra {original_title}"

    subtitle = (
        f"Uoffisiell feed med de siste {ep_i} episodene "
        f"fra podkasten {original_title}. "
        f"Opphavsrett på innhold eies av NRK og ev. andre "
        f"rettighetshavere. Se {website} for mer informasjon."
    )

    p.name = title
    p.description = subtitle

    return p


def get_item_key(item):
    """
    Return a stable key for an RSS item.

    Prefer GUID, then enclosure URL.
    """

    guid = item.find("guid")

    if guid is not None and guid.text:
        return guid.text.strip()

    enclosure = item.find("enclosure")

    if enclosure is not None:
        url = enclosure.get("url")

        if url:
            return url.strip()

    title = item.find("title")

    if title is not None and title.text:
        return title.text.strip()

    return None


def get_item_date(item):
    """
    Return the publication date as a datetime for sorting.
    """

    pub_date = item.find("pubDate")

    if pub_date is None or not pub_date.text:
        return parser.parse("1970-01-01 00:00:00+00:00")

    try:
        return parsedate_to_datetime(pub_date.text)
    except Exception:
        try:
            return parser.parse(pub_date.text)
        except Exception:
            return parser.parse(
                "1970-01-01 00:00:00+00:00"
            )


def update_feed_metadata(channel, new_channel, item_count):
    """
    Update the channel metadata while preserving the existing
    archive and its RSS structure.
    """

    old_title = channel.find("title")

    if old_title is not None and old_title.text:
        match = re.match(
            r"De \d+ siste fra (.+)",
            old_title.text,
        )

        if match:
            original_title = match.group(1)
            old_title.text = (
                f"De {item_count} siste fra {original_title}"
            )

    old_description = channel.find("description")

    if old_description is not None and old_description.text:
        old_description.text = re.sub(
            r"de siste \d+ episodene",
            f"de siste {item_count} episodene",
            old_description.text,
            count=1,
        )

    new_build_date = new_channel.find("lastBuildDate")

    if new_build_date is not None:
        old_build_date = channel.find("lastBuildDate")

        if old_build_date is not None:
            old_build_date.text = new_build_date.text


def write_podcast_xml(
    feeds_dir,
    podcast_id,
    podcast,
    archive_mode=False,
):
    output_path = f"{feeds_dir}/{podcast_id}.xml"

    existing_tree = None

    if os.path.exists(output_path):
        try:
            existing_tree = ET.parse(output_path)
        except Exception:
            logging.warning(
                f"Could not parse existing feed: {output_path}"
            )
            existing_tree = None

    # Generate the new episodes into a temporary XML file.
    with tempfile.NamedTemporaryFile(
        suffix=".xml",
        delete=False,
    ) as temp_file:
        temp_path = temp_file.name

    try:
        podcast.rss_file(
            temp_path,
            minimize=False,
        )

        new_tree = ET.parse(temp_path)
        new_root = new_tree.getroot()
        new_channel = new_root.find("channel")

        # -----------------------------------------------------
        # No existing feed:
        #
        # This is the first full archive generation.
        # -----------------------------------------------------

        if existing_tree is None:
            if archive_mode:
                marker = ET.Element(
                    f"{{{ARCHIVE_NS}}}archiveInitialized"
                )
                marker.text = "true"
                new_channel.append(marker)

            new_tree.write(
                output_path,
                encoding="UTF-8",
                xml_declaration=True,
            )

            logging.info(
                f"Podcast XML successfully written to file: "
                f"{output_path}\n---"
            )

            return output_path

        # -----------------------------------------------------
        # Existing feed:
        #
        # Merge the newly fetched episodes into the old feed.
        # -----------------------------------------------------

        old_root = existing_tree.getroot()
        old_channel = old_root.find("channel")

        if old_channel is None or new_channel is None:
            logging.warning(
                "Could not find RSS channel while merging feed"
            )
            return None

        existing_keys = set()

        for item in old_channel.findall("item"):
            key = get_item_key(item)

            if key:
                existing_keys.add(key)

        added = 0

        for new_item in new_channel.findall("item"):
            key = get_item_key(new_item)

            if key and key in existing_keys:
                continue

            old_channel.append(new_item)
            added += 1

            if key:
                existing_keys.add(key)

        # Sort all episodes newest first.
        items = old_channel.findall("item")

        items.sort(
            key=get_item_date,
            reverse=True,
        )

        for item in old_channel.findall("item"):
            old_channel.remove(item)

        for item in items:
            old_channel.append(item)

        item_count = len(items)

        update_feed_metadata(
            old_channel,
            new_channel,
            item_count,
        )

        # Mark the archive as initialized.
        if archive_mode:
            marker = old_channel.find(
                f"{{{ARCHIVE_NS}}}archiveInitialized"
            )

            if marker is None:
                marker = ET.Element(
                    f"{{{ARCHIVE_NS}}}archiveInitialized"
                )
                marker.text = "true"
                old_channel.append(marker)
            else:
                marker.text = "true"

        existing_tree.write(
            output_path,
            encoding="UTF-8",
            xml_declaration=True,
        )

        logging.info(
            f"Podcast XML successfully updated: "
            f"{output_path}"
        )

        logging.info(
            f"  Total episodes in feed: {item_count}"
        )

        logging.info(
            f"  New episodes added: {added}"
        )

        logging.info("---")

        return output_path

    finally:
        try:
            os.remove(temp_path)
        except OSError:
            pass


if __name__ == "__main__":
    init()

    feeds_dir = "docs/rss"
    feeds_file = "docs/feeds.js"

    podcasts = get_podcasts_config(
        podcasts_cfg_file
    )

    for p in podcasts:
        if not p["enabled"]:
            continue

        podcast_id = p["id"]
        podcast_season = p["season"]

        ep_count = 10

        if "episodes" in p:
            ep_count = p["episodes"]

        podcast = get_podcast(
            podcast_id,
            podcast_season,
            feeds_dir,
            ep_count,
        )

        if not podcast:
            logging.debug(
                f"Got empty result when fetching podcast "
                f"{podcast_id}"
            )
            continue

        write_podcast_xml(
            feeds_dir,
            podcast_id,
            podcast,
            archive_mode=(ep_count == 0),
        )

    write_feeds_file(
        feeds_file,
        podcasts,
    )

    logging.info("Done")
