from flask import Flask, request, jsonify
import sys
import os
from dotenv import load_dotenv
import ipaddress
import socket
from urllib.parse import urlparse
import os
from worker import (
    scrape_task, 
    MAX_BROWSER_DIM, 
    MIN_BROWSER_DIM, 
    DEFAULT_BROWSER_DIM, 
    DEFAULT_WAIT, 
    MAX_SCREENSHOTS, 
    MAX_WAIT, 
    DEFAULT_SCREENSHOTS,
    MEM_LIMIT_MB,
    MAX_CONCURRENT_TASKS,
    SCREENSHOT_QUALITY,
    USER_AGENT
)
import json
import mimetypes


app = Flask(__name__)

"""

Flask server runs and gets requests to scrape.

The server worker process spawned by gunicorn itself maintains a separate pool of scraping workers (there should be just one server worker - see Dockerfile).

Upon a request to /scrape, the gunicorn worker asks the pool for a process to run a scrape, which spawns an isolated browser context.

The scrape workers' memory usage and number are limited by constants set in worker.py.

"""

# For optional API key
load_dotenv()  # Load in API keys
SCRAPER_API_KEYS = [value for key, value in os.environ.items() if key.startswith('SCRAPER_API_KEY')]


# Print server options on startup
def print_server_options():
    print("=" * 60, file=sys.stderr)
    print("ScrapeServ - Server Options", file=sys.stderr)
    print("=" * 60, file=sys.stderr)
    print(f"MEM_LIMIT_MB:           {MEM_LIMIT_MB}", file=sys.stderr)
    print(f"MAX_CONCURRENT_TASKS:   {MAX_CONCURRENT_TASKS}", file=sys.stderr)
    print(f"DEFAULT_SCREENSHOTS:    {DEFAULT_SCREENSHOTS}", file=sys.stderr)
    print(f"MAX_SCREENSHOTS:        {MAX_SCREENSHOTS}", file=sys.stderr)
    print(f"DEFAULT_WAIT:           {DEFAULT_WAIT} ms", file=sys.stderr)
    print(f"MAX_WAIT:               {MAX_WAIT} ms", file=sys.stderr)
    print(f"SCREENSHOT_QUALITY:     {SCREENSHOT_QUALITY}", file=sys.stderr)
    print(f"DEFAULT_BROWSER_DIM:    {DEFAULT_BROWSER_DIM[0]}x{DEFAULT_BROWSER_DIM[1]} (W x H)", file=sys.stderr)
    print(f"MAX_BROWSER_DIM:        {MAX_BROWSER_DIM[0]}x{MAX_BROWSER_DIM[1]} (W x H)", file=sys.stderr)
    print(f"MIN_BROWSER_DIM:        {MIN_BROWSER_DIM[0]}x{MIN_BROWSER_DIM[1]} (W x H)", file=sys.stderr)
    print(f"USER_AGENT:             {USER_AGENT}", file=sys.stderr)
    print(f"API Keys configured:    {len(SCRAPER_API_KEYS)}", file=sys.stderr)
    print("=" * 60, file=sys.stderr)
    sys.stderr.flush()


# Print options when the module is loaded
print_server_options()


@app.route('/')
def home():
    return "A rollicking band of pirates we, who tired of tossing on the sea, are trying our hands at burglary, with weapons grim and gory."


def is_private_ip(ip_str: str) -> bool:
    """
    Checks if the given IP address string (e.g., '10.0.0.1', '127.0.0.1')
    is private, loopback, or link-local.
    """
    try:
        ip_obj = ipaddress.ip_address(ip_str)
        return (
            ip_obj.is_loopback or
            ip_obj.is_private or
            ip_obj.is_reserved or
            ip_obj.is_link_local or
            ip_obj.is_multicast
        )
    except ValueError:
        return True  # If it can't parse, treat as "potentially unsafe"


def url_is_safe(url: str, allowed_schemes=None) -> bool:
    if allowed_schemes is None:
        # By default, let's only allow http(s)
        allowed_schemes = {"http", "https"}

    # Parse the URL
    parsed = urlparse(url.strip())
    scheme = parsed.scheme.lower()
    netloc = parsed.netloc.split(':')[0]  # extract host portion w/o port
    
    print(f"[SECURITY CHECK] Validating URL: {url}", file=sys.stderr)
    print(f"[SECURITY CHECK] Parsed - scheme: {scheme}, netloc: {netloc}", file=sys.stderr)
    
    if scheme not in allowed_schemes:
        print(f"[SECURITY CHECK] ❌ BLOCKED: scheme '{scheme}' is not allowed (allowed: {', '.join(allowed_schemes)})", file=sys.stderr)
        return False
    
    print(f"[SECURITY CHECK] ✓ Scheme '{scheme}' is allowed", file=sys.stderr)

    try:
        # Resolve the domain name to IP addresses
        # This can raise socket.gaierror if domain does not exist
        print(f"[SECURITY CHECK] Resolving domain: {netloc}", file=sys.stderr)
        addrs = socket.getaddrinfo(netloc, None)
        print(f"[SECURITY CHECK] ✓ Domain resolved to {len(addrs)} address(es)", file=sys.stderr)
    except socket.gaierror as e:
        print(f"[SECURITY CHECK] ❌ BLOCKED: cannot resolve domain {netloc} - {e}", file=sys.stderr)
        return False

    # Check each resolved address
    for i, addrinfo in enumerate(addrs):
        ip_str = addrinfo[4][0]
        print(f"[SECURITY CHECK] Checking IP #{i+1}: {ip_str}", file=sys.stderr)
        if is_private_ip(ip_str):
            print(f"[SECURITY CHECK] ❌ BLOCKED: IP {ip_str} for domain {netloc} is private/loopback/reserved/link-local/multicast", file=sys.stderr)
            return False
        print(f"[SECURITY CHECK] ✓ IP {ip_str} is public", file=sys.stderr)

    # If all resolved IPs appear safe, pass it
    print(f"[SECURITY CHECK] ✓ All checks passed for {netloc}", file=sys.stderr)
    return True


# Includes dot
def get_ext_from_content_type(content_type: str):
    mime_type = content_type.split(';')[0].strip()
    extensions = mimetypes.guess_all_extensions(mime_type)
    if len(extensions):
        return f"{extensions[0]}"
    return ""


@app.route('/scrape', methods=('POST',))
def scrape():
    # Log incoming request
    print("=" * 80, file=sys.stderr)
    print(f"[REQUEST] New scrape request from {request.remote_addr}", file=sys.stderr)
    print(f"[REQUEST] Time: {__import__('datetime').datetime.now().isoformat()}", file=sys.stderr)
    
    if len(SCRAPER_API_KEYS):
        auth_header = request.headers.get('Authorization')
        if auth_header is None:
            print(f"[AUTH] ❌ Authorization header missing", file=sys.stderr)
            print("=" * 80, file=sys.stderr)
            sys.stderr.flush()
            return jsonify({"error": "Authorization header is missing"}), 401

        if not auth_header.startswith('Bearer '):
            print(f"[AUTH] ❌ Invalid authorization header format", file=sys.stderr)
            print("=" * 80, file=sys.stderr)
            sys.stderr.flush()
            return jsonify({"error": "Invalid authorization header format"}), 401

        user_key = auth_header.split(' ')[1]
        if user_key not in SCRAPER_API_KEYS:
            print(f"[AUTH] ❌ Invalid API key provided", file=sys.stderr)
            print("=" * 80, file=sys.stderr)
            sys.stderr.flush()
            return jsonify({'error': 'Invalid API key'}), 401
        
        print(f"[AUTH] ✓ Valid API key", file=sys.stderr)
    else:
        print(f"[AUTH] No API keys configured (public mode)", file=sys.stderr)

    url = request.json.get('url')

    if not url:
        print(f"[ERROR] ❌ No URL provided in request", file=sys.stderr)
        print("=" * 80, file=sys.stderr)
        sys.stderr.flush()
        return jsonify({'error': 'No URL provided'}), 400
    
    print(f"[URL] Requested URL: {url}", file=sys.stderr)
    
    if not url_is_safe(url):
        print(f"[SECURITY] ❌ URL rejected as unsafe: {url}", file=sys.stderr)
        print("=" * 80, file=sys.stderr)
        sys.stderr.flush()
        return jsonify({'error': 'URL was judged to be unsafe'}), 400
    
    print(f"[SECURITY] ✓ URL passed safety checks", file=sys.stderr)

    wait = request.json.get('wait', DEFAULT_WAIT)
    n_screenshots = request.json.get('max_screenshots', DEFAULT_SCREENSHOTS)
    browser_dim = request.json.get('browser_dim', DEFAULT_BROWSER_DIM)

    print(f"[PARAMS] wait={wait}ms, max_screenshots={n_screenshots}, browser_dim={browser_dim[0]}x{browser_dim[1]}", file=sys.stderr)

    if wait < 0 or wait > MAX_WAIT:
        print(f"[VALIDATION] ❌ Invalid wait value: {wait} (must be 0-{MAX_WAIT})", file=sys.stderr)
        print("=" * 80, file=sys.stderr)
        sys.stderr.flush()
        return jsonify({
            'error': f'Value {wait} for "wait" is unacceptable; must be between 0 and {MAX_WAIT}'
        }), 400
    
    for i, name in enumerate(['width', 'height']):
        if browser_dim[i] > MAX_BROWSER_DIM[i] or browser_dim[i] < MIN_BROWSER_DIM[i]:
            print(f"[VALIDATION] ❌ Invalid browser {name}: {browser_dim[i]} (must be {MIN_BROWSER_DIM[i]}-{MAX_BROWSER_DIM[i]})", file=sys.stderr)
            print("=" * 80, file=sys.stderr)
            sys.stderr.flush()
            return jsonify({
                'error': f'Value {browser_dim[i]} for browser {name} is unacceptable; must be between {MIN_BROWSER_DIM[i]} and {MAX_BROWSER_DIM[i]}'
            }), 400
        
    if n_screenshots > MAX_SCREENSHOTS:
        print(f"[VALIDATION] ❌ Invalid max_screenshots: {n_screenshots} (must be ≤{MAX_SCREENSHOTS})", file=sys.stderr)
        print("=" * 80, file=sys.stderr)
        sys.stderr.flush()
        return jsonify({
                'error': f'Value {n_screenshots} for max_screenshots is unacceptable; must be below {MAX_SCREENSHOTS}'
            }), 400
    
    # Determine the image format from the Accept header
    accept_header = request.headers.get('Accept', 'image/jpeg')
    accepted_formats = {
        'image/webp': 'webp',
        'image/png': 'png',
        'image/jpeg': 'jpeg',
        'image/*': 'jpeg',
        '*/*': 'jpeg'
    }

    image_format = accepted_formats.get(accept_header)
    if not image_format:
        accepted_formats_list = ', '.join(accepted_formats.keys())
        print(f"[VALIDATION] ❌ Unsupported image format: {accept_header}", file=sys.stderr)
        print("=" * 80, file=sys.stderr)
        sys.stderr.flush()
        return jsonify({
            'error': f'Unsupported image format in Accept header ({accept_header}). Supported Accept header values are: {accepted_formats_list}'
        }), 406
    
    print(f"[FORMAT] Image format: {image_format}", file=sys.stderr)

    content_file = None
    try:
        print(f"[SCRAPING] Starting scrape task for: {url}", file=sys.stderr)
        sys.stderr.flush()
        
        status, headers, content_file, screenshot_files, metadata = scrape_task.apply_async(
            args=[url, wait, image_format, n_screenshots, browser_dim], kwargs={}
        ).get(timeout=60)  # 60 seconds
        headers = {str(k).lower(): v for k, v in headers.items()}  # make headers all lowercase (they're case insensitive)
        
        print(f"[SCRAPING] ✓ Scrape completed successfully", file=sys.stderr)
        print(f"[RESULT] Status: {status}, Screenshots: {len(screenshot_files)}, Content-Type: {headers.get('content-type', 'unknown')}", file=sys.stderr)
    except Exception as e:
        # If scrape_in_child uses too much memory, it seems to end up here.
        # however, if exit(0) is called, I find it doesn't.
        print(f"[SCRAPING] ❌ Exception raised from scraping process: {e}", file=sys.stderr, flush=True)

    successful = True if content_file else False

    if successful:
        boundary = 'Boundary712sAM12MVaJff23NXJ'  # typed out some random digits
        # Generate a mixed multipart response
        # See details on the standard here: https://www.w3.org/Protocols/rfc1341/7_2_Multipart.html
        def stream():
            # Start with headers and status as json
            # JSON part with filename
            filename = "info.json"
            yield (
                f"--{boundary}\r\n"
                "Content-Type: application/json\r\n"
                f"Content-Disposition: attachment; name=\"{filename}\"; filename=\"{filename}\"\r\n\r\n"
            ).encode()
            yield json.dumps({'status': status, 'headers': headers, 'metadata': metadata}).encode()

            # Main content (HTML/other)
            ext = get_ext_from_content_type(headers['content-type'])
            filename = f"main{ext}"
            yield (
                f"\r\n--{boundary}\r\n"
                f"Content-Disposition: attachment; name=\"{filename}\"; filename=\"{filename}\"\r\n"
                "Content-Transfer-Encoding: binary\r\n"
                f"Content-Type: {headers['content-type']}\r\n\r\n"
            ).encode()
            with open(content_file, 'rb') as content:
                while chunk := content.read(4096):
                    yield chunk

            # Screenshots (correct MIME type)
            for i, ss in enumerate(screenshot_files):
                filename = f"ss{i}.{image_format}"
                yield (
                    f"\r\n--{boundary}\r\n"
                    f"Content-Disposition: attachment; name=\"{filename}\"; filename=\"{filename}\"\r\n"
                    "Content-Transfer-Encoding: binary\r\n"
                    f"Content-Type: image/{image_format}\r\n\r\n"
                ).encode()
                with open(ss, 'rb') as content:
                    while chunk := content.read(4096):
                        yield chunk

            # Final boundary
            yield f"\r\n--{boundary}--\r\n".encode()

        print(f"[RESPONSE] ✓ Sending multipart response with {len(screenshot_files)} screenshots", file=sys.stderr)
        print("=" * 80, file=sys.stderr)
        sys.stderr.flush()
        
        return stream(), 200, {'Content-Type': f'multipart/mixed; boundary={boundary}'}

    else:
        print(f"[RESPONSE] ❌ Request failed - returning error response", file=sys.stderr)
        print("=" * 80, file=sys.stderr)
        sys.stderr.flush()
        
        return jsonify({
            'error': "This is a generic error message; sorry about that."
        }), 500
