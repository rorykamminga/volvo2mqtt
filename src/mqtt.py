import logging
import time
import paho.mqtt.client as mqtt
import json
import volvo
import util
import os
from threading import Thread, Timer
from datetime import datetime
from babel.dates import format_datetime
from config import settings
from const import CLIMATE_START_URL, CLIMATE_STOP_URL, CAR_LOCK_URL, \
            CAR_UNLOCK_URL, availability_topic, icon_states


mqtt_client: mqtt.Client
subscribed_topics = []
assumed_climate_state = {}
last_data_update = None
climate_timer = {}
engine_status = {}
devices = {}
active_schedules = {}


def connect():
    client = mqtt.Client("volvoAAOS2mqtt") if os.environ.get("IS_HA_ADDON") \
        else mqtt.Client("volvoAAOS2mqtt_" + settings.volvoData["username"].replace("+", ""))

    client.will_set(availability_topic, "offline", 0, False)
    if settings["mqtt"]["username"] and settings["mqtt"]["password"]:
        client.username_pw_set(settings["mqtt"]["username"], settings["mqtt"]["password"])
    port = 1883
    if util.keys_exists(settings["mqtt"], "port"):
        conf_port = settings["mqtt"]["port"]
        if isinstance(conf_port, int):
            if conf_port > 0:
                port = settings["mqtt"]["port"]
    client.connect(settings["mqtt"]["broker"], port)
    client.loop_start()
    client.on_message = on_message
    client.on_disconnect = on_disconnect
    client.on_connect = on_connect

    global mqtt_client
    mqtt_client = client


def send_car_images(vin, data, device):
    if util.keys_exists(data, "images"):
        for entity in [{"name": "Exterior Image", "id": "exterior_image"},
                       {"name": "Interior Image", "id": "interior_image"}]:
            url_topic = f"homeassistant/image/{vin}_{entity['id']}/image_url"
            config = {
                "name": entity["name"],
                "object_id": f"volvo_{vin}_{entity['id']}",
                "schema": "state",
                "icon": "mdi:image-area",
                "url_topic": url_topic,
                "device": device,
                "unique_id": f"volvoAAOS2mqtt_{vin}_{entity['id']}",
                "availability_topic": availability_topic
            }

            mqtt_client.publish(
                f"homeassistant/image/volvoAAOS2mqtt/{vin}_{entity['id']}/config",
                json.dumps(config),
                retain=True
            )

            if entity["id"] == "exterior_image":
                mqtt_client.publish(
                    url_topic,
                    data["images"]["exteriorDefaultUrl"],
                    retain=True
                )

            if entity["id"] == "interior_image":
                mqtt_client.publish(
                    url_topic,
                    data["images"]["interiorDefaultUrl"],
                    retain=True
                )


def on_connect(client, userdata, flags, rc):
    send_heartbeat()
    if len(subscribed_topics) > 0:
        for topic in subscribed_topics:
            mqtt_client.subscribe(topic)


def on_disconnect(client, userdata,  rc):
    logging.warning("MQTT disconnected, reconnecting automatically")


def on_message(client, userdata, msg):
    try:
        vin = msg.topic.split('/')[2].split('_')[0]
    except IndexError:
        logging.error("Error - Cannot get vin from MQTT topic!")
        return None

    payload = msg.payload.decode("UTF-8")
    if "climate_status" in msg.topic:
        if payload == "ON":
            start_climate(vin)
        elif payload == "OFF":
            stop_climate(vin)
    elif "lock_status" in msg.topic:
        if payload == "LOCK":
            lock_car(vin)
        elif payload == "UNLOCK":
            unlock_car(vin)
    elif "update_data" in msg.topic:
        if payload == "PRESS":
            update_car_data(True)
    elif "schedule" in msg.topic:
        try:
            d = json.loads(payload)
        except ValueError as e:
            logging.error("Can't set timer. Error: " + str(e))
            return None

        if d["mode"] == "timer":
            start_climate_timer(d, vin)
        else:
            logging.warning("No schedule mode found, doing nothing")


def start_climate_timer(d, vin):
    global active_schedules
    try:
        minute = int(d["start_time"].split(":")[1])
        hour = int(d["start_time"].split(":")[0])
        local_datetime = datetime.now(util.TZ)
        start_datetime = local_datetime.replace(hour=hour, minute=minute, second=0)
        timer_seconds = (start_datetime - local_datetime).total_seconds()
    except Exception as e:
        logging.error("Error creating climate timer: " + str(e))
        return None

    if timer_seconds > 0:
        Timer(timer_seconds, activate_climate_timer, (vin, start_datetime.isoformat(), )).start()
        active_schedules[vin]["timers"].append(start_datetime.isoformat())
        logging.debug("Climate timer set to " + str(start_datetime))
        update_car_data()
    else:
        logging.warning("Timer can not be set. Unusable start time entered")


def unlock_car(vin):
    # Start the api call in another thread for HA performance
    Thread(target=volvo.api_call, args=(CAR_UNLOCK_URL, "POST", vin)).start()

    # Force set unlocking state
    update_car_data(False, {"entity_id": "lock_status", "vin": vin, "state": "UNLOCKING"})
    # Fetch API lock state until unlocking finished
    Thread(target=volvo.check_lock_status, args=(vin, "LOCKED")).start()


def lock_car(vin):
    # Start the api call in another thread for HA performance
    Thread(target=volvo.api_call, args=(CAR_LOCK_URL, "POST", vin)).start()

    # Force set locking state
    update_car_data(False, {"entity_id": "lock_status", "vin": vin, "state": "LOCKING"})
    # Fetch API lock state until locking finished
    Thread(target=volvo.check_lock_status, args=(vin, "UNLOCKED")).start()


def stop_climate(vin):
    global assumed_climate_state, climate_timer, engine_status
    # Start the api call in another thread for HA performance
    Thread(target=volvo.api_call, args=(CLIMATE_STOP_URL, "POST", vin)).start()

    # Stop door check thread if running
    if engine_status[vin].is_alive():
        engine_status[vin].do_run = False

    # Stop climate timer if active
    if climate_timer[vin].is_alive():
        climate_timer[vin].cancel()

    # Set and update switch status
    assumed_climate_state[vin] = "OFF"
    update_car_data()


def activate_climate_timer(vin, start_time):
    start_climate(vin)
    active_schedules[vin]["timers"].remove(start_time)
    update_car_data()


def start_climate(vin):
    global assumed_climate_state, climate_timer, engine_status
    # Start the api call in another thread for HA performance
    Thread(target=volvo.api_call, args=(CLIMATE_START_URL, "POST", vin)).start()

    # Start door check thread to turn off climate if driver door is opened
    check_engine_thread = Thread(target=volvo.check_engine_status, args=(vin,))
    check_engine_thread.start()
    engine_status[vin] = check_engine_thread

    # Starting timer to disable climate after 30 mins
    climate_timer[vin] = Timer(30 * 60, volvo.disable_climate, (vin,))
    climate_timer[vin].start()

    # Set and update switch status
    assumed_climate_state[vin] = "ON"
    update_car_data()


def update_loop():
    create_ha_devices()
    while True:
        logging.info("Sending mqtt update...")
        send_heartbeat()
        update_car_data()
        logging.info("Mqtt update done. Next run in " + str(settings["updateInterval"]) + " seconds.")
        time.sleep(settings["updateInterval"])


def update_car_data(force_update=False, overwrite={}):
    global last_data_update
    last_data_update = format_datetime(datetime.now(util.TZ), format="medium", locale=settings["babelLocale"])
    for vin in volvo.vins:
        for entity in volvo.supported_endpoints[vin]:
            if entity["domain"] == "button":
                continue

            ov_entity_id = ""
            ov_vin = ""
            ov_state = ""
            if bool(overwrite):
                ov_entity_id = overwrite["entity_id"]
                ov_vin = overwrite["vin"]
                ov_state = overwrite["state"]

            if entity["id"] == "climate_status":
                state = assumed_climate_state[vin]
            elif entity["id"] == "last_data_update":
                state = last_data_update
            elif entity["id"] == "active_schedules":
                state = active_schedules[vin]
            else:
                if entity["id"] == ov_entity_id and vin == ov_vin:
                    state = ov_state
                else:
                    state = volvo.api_call(entity["url"], "GET", vin, entity["id"], force_update)

            if entity["domain"] == "device_tracker" or entity["id"] == "active_schedules":
                topic = f"homeassistant/{entity['domain']}/{vin}_{entity['id']}/attributes"
            else:
                topic = f"homeassistant/{entity['domain']}/{vin}_{entity['id']}/state"

            if state:
                mqtt_client.publish(
                    topic,
                    json.dumps(state) if isinstance(state, dict) or isinstance(state, list) else state
                )
                update_ha_device(entity, vin, state)


def update_ha_device(entity, vin, state):
    icon_config = icon_states.get(entity["id"])
    if icon_config and state:
        if state.replace(".", "").isnumeric():
            state = float(state)
            icon = util.get_icon_between(icon_config, state)
        else:
            icon = icon_config[state]
    else:
        return None

    logging.debug("Updating icon to " + icon + " for " + entity["id"])
    config = {
        "name": entity['name'],
        "object_id": f"volvo_{vin}_{entity['id']}",
        "schema": "state",
        "icon": f"mdi:{icon}" if icon else f"mdi:{entity['icon']}",
        "state_topic": f"homeassistant/{entity['domain']}/{vin}_{entity['id']}/state",
        "device": devices[vin],
        "unique_id": f"volvoAAOS2mqtt_{vin}_{entity['id']}",
        "availability_topic": availability_topic
    }
    if entity.get("device_class"):
        config["device_class"] = entity["device_class"]

    if entity.get("unit"):
        config["unit_of_measurement"] = entity["unit"]

    if entity.get("state_class"):
        config["state_class"] = entity["state_class"]
        
    if entity.get("domain") == "device_tracker":
        config["json_attributes_topic"] = f"homeassistant/{entity['domain']}/{vin}_{entity['id']}/attributes"

    if entity.get("domain") in ["switch", "lock", "button"]:
        command_topic = f"homeassistant/{entity['domain']}/{vin}_{entity['id']}/command"
        config["command_topic"] = command_topic
        subscribed_topics.append(command_topic)
        mqtt_client.subscribe(command_topic)

    mqtt_client.publish(
        f"homeassistant/{entity['domain']}/volvoAAOS2mqtt/{vin}_{entity['id']}/config",
        json.dumps(config),
        retain=True
    )


def create_ha_devices():
    global subscribed_topics, devices
    for vin in volvo.vins:
        device = volvo.get_vehicle_details(vin)
        devices[vin] = device
        for entity in volvo.supported_endpoints[vin]:
            config = {
                        "name": entity['name'],
                        "object_id": f"volvo_{vin}_{entity['id']}",
                        "schema": "state",
                        "icon": f"mdi:{entity['icon']}",
                        "state_topic": f"homeassistant/{entity['domain']}/{vin}_{entity['id']}/state",
                        "device": device,
                        "unique_id": f"volvoAAOS2mqtt_{vin}_{entity['id']}",
                        "availability_topic": availability_topic
                    }
            if entity.get("device_class"):
                config["device_class"] = entity["device_class"]

            if entity.get("unit"):
                config["unit_of_measurement"] = entity["unit"]

            if entity.get("state_class"):
                config["state_class"] = entity["state_class"]

            if entity.get("domain") == "device_tracker" or entity.get("id") == "active_schedules":
                config["json_attributes_topic"] = f"homeassistant/{entity['domain']}/{vin}_{entity['id']}/attributes"
            elif entity.get("domain") in ["switch", "lock", "button"]:
                command_topic = f"homeassistant/{entity['domain']}/{vin}_{entity['id']}/command"
                config["command_topic"] = command_topic
                subscribed_topics.append(command_topic)
                mqtt_client.subscribe(command_topic)
            elif entity.get("domain") == "image":
                config["url_topic"] = f"homeassistant/{entity['domain']}/{vin}_{entity['id']}/image_url"

            mqtt_client.publish(
                f"homeassistant/{entity['domain']}/volvoAAOS2mqtt/{vin}_{entity['id']}/config",
                json.dumps(config),
                retain=True
            )
    time.sleep(2)
    send_heartbeat()


def send_heartbeat():
    mqtt_client.publish(availability_topic, "online")


def send_offline():
    mqtt_client.publish(availability_topic, "offline")

