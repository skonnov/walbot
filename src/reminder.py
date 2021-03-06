class Reminder:
    def __init__(self, time, message, channel_id):
        self.time = time
        self.message = message
        self.channel_id = channel_id
        self.ping_users = []
        self.whisper_users = []
        self.repeat_after = 0

    def __eq__(self, time):
        return self.time == time

    def __lt__(self, time):
        return self.time < time

    def __gt__(self, time):
        return self.time > time
