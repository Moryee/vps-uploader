from tldextract import extract
import socket


def get_ip_from_url(url):
    ext = extract(url)

    if not ext.suffix:
        return ext.domain

    subdomain = ext.subdomain if ext.subdomain else 'www'
    return socket.gethostbyname(subdomain + '.' + ext.domain + '.' + ext.suffix)


def format_download_time(seconds: float):
    return round(seconds * 1000, 3)


def calculate_downloading_speed(speed):
    return speed * 1024 * 1024 / 8
