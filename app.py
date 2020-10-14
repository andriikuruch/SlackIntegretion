from flask import Flask, request, make_response, redirect, jsonify
import os
from slackeventsapi import SlackEventAdapter
from flask_sqlalchemy import SQLAlchemy
from slack.web.client import WebClient
from slack.errors import SlackApiError
from pprint import pprint
from requests import post

client_id = os.environ["SLACK_CLIENT_ID"]
client_secret = os.environ["SLACK_CLIENT_SECRET"]
slack_app_id = os.environ['SLACK_APP_ID']
db_username = os.environ["DB_USERNAME"]
db_password = os.environ["DB_PASSWORD"]
db_host = os.environ["DB_HOST"]
db_port = os.environ["DB_PORT"]
db_name = os.environ["DB_NAME"]

app = Flask(__name__)
app.config['SQLALCHEMY_DATABASE_URI'] = f"postgres://{db_username}:{db_password}@{db_host}:{db_port}/{db_name}"
slack_event_adapter = SlackEventAdapter(os.environ["SLACK_SIGNING_SECRET"], "/slack/event", app)
db = SQLAlchemy(app)


class SlackInfo(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    access_token = db.Column(db.String, unique=True, nullable=False)
    bot_access_token = db.Column(db.String, unique=True, nullable=False)
    team_name = db.Column(db.String, nullable=False)
    team_id = db.Column(db.String, nullable=False)
    user_id = db.Column(db.String, nullable=False)


db.create_all()


@app.route("/message/send", methods=["POST"])
def send_message():
    slack_info = SlackInfo.query.filter_by(team_name=request.get_json()["team"]).first()

    if slack_info is None:
        return jsonify({
            "type": "not_found",
            "text": "Team not found"
        }), 400

    slack = WebClient(token=slack_info.bot_access_token)

    try:
        slack.chat_postMessage(
            channel=request.get_json()['channel'],
            text=request.get_json()['text']
        )
    except SlackApiError as error:
        if error.response.data["error"] == "channel_not_found":
            return jsonify({
                "type": "not_found",
                "text": "Channel not found"
            }), 400
        elif error.response.data["error"] == "not_in_channel":
            return jsonify({
                "type": "not_in_channel",
                "text": "The bot is not a member of the channel"
            }), 400

    return make_response("", 200)


@app.route("/messages", methods=["GET"])
def get_messages():
    team = request.args.get("team", type=str)
    channel_name = request.args.get("channel", type=str)
    start_time = request.args.get("from", type=str)
    end_time = request.args.get("to", type=str)

    slack_info = SlackInfo.query.filter_by(team_name=team).first()

    if slack_info is None:
        return jsonify({
            "type": "not_found",
            "text": "Team not found"
        }), 400

    slack = WebClient(slack_info.bot_access_token)

    channel_list = slack.conversations_list(token=slack_info.bot_access_token, types="public_channel, private_channel")

    try:
        channel = next(
            filter(lambda channel: channel["name"] == channel_name, channel_list["channels"])
        )

        message_history = slack.conversations_history(
            channel=channel["id"],
            token=slack_info.bot_access_token,
            oldest=start_time,
            latest=end_time
        )
    except StopIteration:
        return jsonify({
            "type": "not_found",
            "text": "Channel not found"
        }), 400
    except SlackApiError:
        return jsonify({
            "type": "not_in_channel",
            "text": "The bot is not a member of the channel"
        }), 400

    response_keys = ["text", "sender", "time"]
    request_keys = ["text", "user", "ts"]

    response = [
        {response_key: message[request_key] for response_key, request_key in zip(response_keys, request_keys)}
        for message in message_history["messages"]
    ]

    for message, item in zip(message_history["messages"], response):

        if "thread_ts" in message:

            # Get thread messages and delete parent message
            thread_messages = slack.conversations_replies(channel=channel["id"], ts=message["thread_ts"]).data["messages"][1::]

            item["thread"] = [
                {response_key: thread_message[request_key] for response_key, request_key in zip(response_keys, request_keys)}
                for thread_message in thread_messages
            ]
        else:
            item["thread"] = []

        # Replace user ID with username
        item["sender"] = slack.users_info(token=slack_info.bot_access_token, user=item["sender"])["user"]["name"]

        for thread in item["thread"]:
            # Replace user ID with username in thread's message
            thread["sender"] = slack.users_info(token=slack_info.bot_access_token, user=thread["sender"])["user"]["name"]

    return jsonify(response)


@app.route('/message/echo', methods=["POST"])
def on_echo_command():
    slack_info = SlackInfo.query.filter_by(team_id=request.values["team_id"]).first()

    slack = WebClient(slack_info.bot_access_token)
    pprint(request.values.to_dict())

    try:
        slack.chat_postMessage(
            channel=request.values['channel_id'],
            text=f"{request.values['user_name']} said: {request.values['text']}"
        )
    except SlackApiError:
        post(
            url=request.values["response_url"],
            json={
                "response_type": "ephemeral",
                "text": "The bot *is not* a member of the channel"
            }
        )

    return make_response('', 200)


@app.route("/auth", methods=["GET", "POST"])
def authorize():

    if "error" in request.args:
        return redirect(f"https://slack.com/app_redirect?app={slack_app_id}", code=302)

    auth_code = request.args['code']

    client = WebClient(token="")

    response = client.oauth_v2_access(
        client_id=client_id,
        client_secret=client_secret,
        code=auth_code
    )

    slack_info = SlackInfo(
        access_token=response["authed_user"]["access_token"],
        bot_access_token=response["access_token"],
        team_name=response["team"]["name"],
        team_id=response["team"]["id"],
        user_id=response["authed_user"]["id"]
    )

    db.session.add(slack_info)
    db.session.commit()

    return redirect(f"https://slack.com/app_redirect?team={response['team']['id']}&app={slack_app_id}", code=302)


@slack_event_adapter.on("team_rename")
def on_team_rename(event_data):
    team_id = event_data["team_id"]
    team_name = event_data["event"]["name"]

    SlackInfo.query.filter_by(team_id=team_id).update({'team_name': team_name})
    db.session.commit()


@slack_event_adapter.on("tokens_revoked")
def delete(event_data):
    pprint(event_data)
    team_id = event_data["team_id"]
    user_id = event_data["event"]["tokens"]["oauth"][0]

    SlackInfo.query.filter_by(team_id=team_id, user_id=user_id).delete()
    db.session.commit()


if __name__ == '__main__':
    app.run()
