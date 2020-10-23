import argparse
import base64
import json
import time
import zlib
import os
from collections import namedtuple
from datetime import datetime, timedelta

import polyline
import pytz
import gpxpy
import requests

from config import (
    GPX_FOLDER,
    JSON_FILE,
    NIKE_CLIENT_ID,
    OUTPUT_DIR,
    SQL_FILE,
    TOKEN_REFRESH_URL,
)
from generator import Generator

start_point = namedtuple("start_point", "lat lon")
run_map = namedtuple("polyline", "summary_polyline")

# need to test
LOGIN_API = "https://api.gotokeep.com/v1.1/users/login"
RUN_DATA_API = "https://api.gotokeep.com/pd/v3/stats/detail?dateUnit=all&type=running&lastDate={last_date}"
RUN_LOG_API = "https://api.gotokeep.com/pd/v3/runninglog/{run_id}"


def login(session, mobile, passowrd):
    headers = {
        "User-Agent": "Mozilla/5.0 (X11; Ubuntu; Linux x86_64; rv:78.0) Gecko/20100101 Firefox/78.0",
        "Content-Type": "application/x-www-form-urlencoded;charset=utf-8",
    }
    data = {"mobile": mobile, "password": passowrd}
    r = session.post(LOGIN_API, headers=headers, data=data)
    if r.ok:
        token = r.json()["data"]["token"]
        headers["Authorization"] = f"Bearer {token}"
        return session, headers


def get_to_download_runs_ids(session, headers):
    last_date = 0
    result = []
    while 1:
        r = session.get(RUN_DATA_API.format(last_date=last_date), headers=headers)
        if r.ok:
            run_logs = r.json()["data"]["records"]
            result.extend([i["logs"][0]["stats"]["id"] for i in run_logs])
            last_date = r.json()["data"]["lastTimestamp"]
            since_time = datetime.utcfromtimestamp(last_date / 1000)
            print(f"pares keep ids data since {since_time}")
            time.sleep(1)  # spider rule
            if not last_date:
                break
    return result


def get_single_run_data(session, headers, run_id):
    r = session.get(RUN_LOG_API.format(run_id=run_id), headers=headers)
    if r.ok:
        return r.json()


def decode_runmap_data(text):
    run_points_data = zlib.decompress(base64.b64decode(text), 16 + zlib.MAX_WBITS)
    run_points_data = json.loads(run_points_data)
    return run_points_data


def adjust_time(time, tz_name):
    tc_offset = datetime.now(pytz.timezone(tz_name)).utcoffset()
    return time + tc_offset


def parse_raw_data_to_nametuple(run_data, old_gpx_ids, with_download_gpx=False):
    run_data = run_data["data"]
    run_points_data = []

    # 5898009e387e28303988f3b7_9223370441312156007_rn middle
    keep_id = run_data["id"].split("_")[1]

    start_time = run_data["startTime"]
    if run_data.get("vendor").get("genre", "") == "KeepApp":
        raw_data_url = run_data.get("rawDataURL")
        r = requests.get(raw_data_url)
        # string strart with `H4sIAAAAAAAA` --> decode and unzip
        run_points_data = decode_runmap_data(r.text)
        if with_download_gpx:
           gpx_data = parse_points_to_gpx(run_points_data, start_time)
           if str(keep_id) not in old_gpx_ids:
               download_keep_gpx(gpx_data, str(keep_id))
        run_points_data = [[p["latitude"], p["longitude"]] for p in run_points_data]
    heart_rate = run_data["heartRate"].get("averageHeartRate", None)
    polyline_str = polyline.encode(run_points_data) if run_points_data else ""
    start_latlng = start_point(*run_points_data[0]) if run_points_data else None
    start_date = datetime.utcfromtimestamp(start_time / 1000)
    tz_name = run_data.get("timezone", "")
    start_date_local = adjust_time(start_date, tz_name)
    end = datetime.utcfromtimestamp(run_data["endTime"] / 1000)
    end_local = adjust_time(end, tz_name)
    d = {
        "id": int(keep_id),
        "name": "run from keep",
        # future to support others workout now only for run
        "type": "Run",
        "start_date": datetime.strftime(start_date, "%Y-%m-%d %H:%M:%S"),
        "end": datetime.strftime(end, "%Y-%m-%d %H:%M:%S"),
        "start_date_local": datetime.strftime(start_date_local, "%Y-%m-%d %H:%M:%S"),
        "end_local": datetime.strftime(end_local, "%Y-%m-%d %H:%M:%S"),
        "length": run_data["distance"],
        "average_heartrate": int(heart_rate) if heart_rate else None,
        "map": run_map(polyline_str),
        "start_latlng": start_latlng,
        "distance": run_data["distance"],
        "moving_time": timedelta(seconds=run_data["duration"]),
        "elapsed_time": timedelta(
            seconds=int((run_data["endTime"] - run_data["startTime"]) / 1000)
        ),
        "average_speed": run_data["distance"] / run_data["duration"],
        "location_country": str(run_data.get("region", "")),
    }
    return namedtuple("x", d.keys())(*d.values())


def get_all_keep_tracks(email, password, old_tracks_ids, with_download_gpx=False):
    s = requests.Session()
    s, headers = login(s, email, password)
    runs = get_to_download_runs_ids(s, headers)
    runs = [run for run in runs if run.split("_")[1] not in old_tracks_ids]
    print(f"{len(runs)} new keep runs to generate")
    tracks = []
    old_gpx_ids = os.listdir(GPX_FOLDER)
    old_gpx_ids = [i.split(".")[0] for i in old_gpx_ids]
    for run in runs:
        print(f"parsing keep id {run}")
        try:
            run_data = get_single_run_data(s, headers, run)
            track = parse_raw_data_to_nametuple(run_data, old_gpx_ids, with_download_gpx)
            tracks.append(track)
        except Exception as e:
            print(f"Something wrong paring keep id {run}" + str(e))
    return tracks


def run_keep_sync(email, password, with_download_gpx=False):
    generator = Generator(SQL_FILE)
    old_tracks_ids = generator.get_old_tracks_ids()
    new_tracks = get_all_keep_tracks(email, password, old_tracks_ids, with_download_gpx)
    generator.sync_from_keep(new_tracks)

    activities_list = generator.load()
    with open(JSON_FILE, "w") as f:
        f.write("const activities = ")
        json.dump(activities_list, f, indent=2)
        f.write(";\n")
        f.write("\n")
        f.write("export {activities};\n")


def parse_points_to_gpx(run_points_data, start_time):
    # future to support heart rate
    points_dict_list = []
    for point in run_points_data:
        points_dict_list.append(
            {
                "latitude": point["latitude"],
                "longitude": point["longitude"],
                "elevation": point["verticalAccuracy"],
                "time": datetime.utcfromtimestamp((point["timestamp"] * 100 + start_time) / 1000),
            }
        )
    gpx = gpxpy.gpx.GPX()
    gpx.nsmap["gpxtpx"] = "http://www.garmin.com/xmlschemas/TrackPointExtension/v1"
    gpx_track = gpxpy.gpx.GPXTrack()
    gpx_track.name = "gpx from keep" 
    gpx.tracks.append(gpx_track)

    # Create first segment in our GPX track:
    gpx_segment = gpxpy.gpx.GPXTrackSegment()
    gpx_track.segments.append(gpx_segment)
    for p in points_dict_list:
        point = gpxpy.gpx.GPXTrackPoint(**p)
        gpx_segment.points.append(point)

    return gpx.to_xml()


def download_keep_gpx(gpx_data, keep_id):
    try:
        print(f"downloading keep_id {str(keep_id)} gpx")
        file_path = os.path.join(GPX_FOLDER, str(keep_id) + ".gpx")
        with open(file_path, "w") as fb:
            fb.write(gpx_data)
    except:
        print(f"wrong id {keep_id}")
        pass


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("email", help="keep login email")
    parser.add_argument("password", help="keep login password")
    parser.add_argument(
        "--with-gpx",
        dest="with_gpx",
        action="store_true",
        help="get all keep data to gpx and download",
    )
    options = parser.parse_args()
    run_keep_sync(options.email, options.password, options.with_gpx)
