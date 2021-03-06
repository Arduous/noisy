# -*- coding: utf-8 -*-
import argparse
import datetime
import json
import logging
import random
import re
import signal
import time
from urllib.parse import urljoin, urlparse

import requests
from urllib3.exceptions import LocationParseError
from validators.url import url as urlValidator 


class Crawler(object):

    def __init__(self):
        """
        Initializes the Crawl class
        """
        self._config = {}
        self._links = []
        self._start_time = None
        signal.signal(signal.SIGINT, self._signal_sigint_handler)

    class LifecycleManagement(Exception):
        """
        Raised when program lifecycle event happens.
        """
        def __init__(self, event=None):
            """
            Initializes the exception with the user defined reason
            :param event: the event happening in the lifecycle of the program
            """
            Exception.__init__(self)
            self.reason = event

    def _signal_sigint_handler(self, sig, frame):
        # pylint: disable=unused-argument
        raise self.LifecycleManagement("SIGINT received.")

    def _request(self, url):
        """
        Sends a POST/GET requests using a random user agent
        :param url: the url to visit
        :return: the response Requests object
        """
        random_user_agent = random.choice(self._config["user_agents"])
        headers = {"user-agent": random_user_agent}

        response = requests.get(url, headers=headers, timeout=5)
        return response

    @staticmethod
    def _normalize_link(link, root_url):
        """
        Normalizes links extracted from the DOM by making them all absolute, so
        we can request them, for example, turns a "/images" link extracted from
        https://imgur.com to "https://imgur.com/images"
        :param link: link found in the DOM
        :param root_url: the URL the DOM was loaded from
        :return: absolute link
        """
        try:
            parsed_url = urlparse(link)
        except ValueError:
            # urlparse can get confused about urls with the ']'
            # character and thinks it must be a malformed IPv6 URL
            return None
        parsed_root_url = urlparse(root_url)

        # '//' means keep the current protocol used to access this URL
        if link.startswith("//"):
            return "{}://{}{}".format(parsed_root_url.scheme,
                                      parsed_url.netloc, parsed_url.path)

        # possibly a relative path
        if not parsed_url.scheme:
            return urljoin(root_url, link)

        return link

    def _is_blacklisted(self, url):
        """
        Checks is a URL is blacklisted
        :param url: full URL
        :return: boolean indicating whether a URL is blacklisted or not
        """
        try:
            return any(blacklisted_url in url
                       for blacklisted_url in self._config["blacklisted_urls"])
        except UnicodeDecodeError:
            return True

    def _should_accept_url(self, url):
        """
        filters url if it is blacklisted or not valid, we put filtering logic
        here.
        :param url: full url to be checked
        :return: boolean of whether or not the url should be accepted and
        potentially visited
        """
        return url and urlValidator(url) and not self._is_blacklisted(url)

    def _extract_urls(self, body, root_url):
        """
        gathers links to be visited in the future from a web page's body.
        does it by finding "href" attributes in the DOM
        :param body: the HTML body to extract links from
        :param root_url: the root URL of the given body
        :return: list of extracted links
        """
        # ignore links starting with #, no point in re-visiting the same page
        pattern = r"href=[\"'](?!#)(.*?)[\"'].*?"
        urls = re.findall(pattern, str(body))

        normalize_urls = [self._normalize_link(url, root_url) for url in urls]
        filtered_urls = list(filter(self._should_accept_url, normalize_urls))

        return filtered_urls

    def _remove_and_blacklist(self, link):
        """
        Removes a link from our current links list
        and blacklists it so we don't visit it in the future
        :param link: link to remove and blacklist
        """
        if link not in self._links:
            return
        self._config["blacklisted_urls"].append(link)
        del self._links[self._links.index(link)]

    def _browse_from_links(self, depth=0):
        """
        Selects a random link out of the available link list and visits it.
        Blacklists any link that is not responsive or that contains no other
        links.
        Please note that this function is recursive and will keep calling
        itself until a dead end has reached or when we ran out of links
        :param depth: our current link depth
        """
        is_depth_reached = depth >= self._config["max_depth"]
        if not len(self._links) or is_depth_reached:
            logging.debug("Hit a dead end, moving to the next root URL")
            # escape from the recursion, we don't have links to continue
            # or we have reached the max depth
            return

        if self._is_timeout_reached():
            raise self.LifecycleManagement("Timeout reached.")

        random_link = random.choice(self._links)
        
        # sleep for a random amount of time
        time.sleep(random.randrange(self._config["min_sleep"],
                                    self._config["max_sleep"]))
        
        try:
            logging.info("Visiting %s", random_link)
            sub_page = self._request(random_link).content
            sub_links = self._extract_urls(sub_page, random_link)

            # make sure we have more than 1 link to pick from
            if len(sub_links) > 1:
                # extract links from the new page
                self._links = self._extract_urls(sub_page, random_link)
            else:
                # else retry with current link list
                # remove the dead-end link from our list
                self._remove_and_blacklist(random_link)

        except (requests.exceptions.RequestException, UnicodeDecodeError):
            logging.debug("Exception on URL: %s, ", random_link +
                          " removing from list and trying again!")
            self._remove_and_blacklist(random_link)

        self._browse_from_links(depth + 1)

    def load_config_file(self, file_path):
        """
        Loads and decodes a JSON config file, sets the config of the crawler
        instance to the loaded one
        :param file_path: path of the config file
        :return:
        """
        with open(file_path, "r") as config_file:
            config = json.load(config_file)
            self.set_config(config)

    def set_config(self, config):
        """
        Sets the config of the crawler instance to the provided dict
        :param config: dict of configuration options, for example:
        {
            "root_urls": [],
            "blacklisted_urls": [],
            "click_depth": 5
            ...
        }
        """
        self._config = config

    def set_option(self, option, value):
        """
        Sets a specific key in the config dict
        :param option: the option key in the config, for example: "max_depth"
        :param value: value for the option
        """
        self._config[option] = value

    def _is_timeout_reached(self):
        """
        Determines whether the specified timeout has reached, if no timeout
        is specified then return false
        :return: boolean indicating whether the timeout has reached
        """
        # False is set when no timeout is desired
        is_timeout_set = self._config["timeout"] is not False
        end_time = self._start_time\
            + datetime.timedelta(seconds=self._config["timeout"])
        is_timed_out = datetime.datetime.now() >= end_time

        return is_timeout_set and is_timed_out

    def crawl(self):
        """
        Collects links from our root urls, stores them and then calls
        `_browse_from_links` to browse them
        """
        self._start_time = datetime.datetime.now()

        while True:
            url = random.choice(self._config["root_urls"])
            try:
                body = self._request(url).content
                self._links = self._extract_urls(body, url)
                logging.debug("found %d links", len(self._links))
                self._browse_from_links()

            except UnicodeDecodeError:
                logging.warning("Error decoding root url: %s", url)
                self._remove_and_blacklist(url)

            except requests.exceptions.RequestException:
                logging.warning("Error connecting to root url: %s", url)

            except MemoryError:
                logging.warning("Error: content at url: %s ", url +
                                "is exhausting the memory")

            except LocationParseError:
                logging.warning("Error encountered during parsing of: %s", url)

            except self.LifecycleManagement as e:
                logging.info("Exiting with reason: %s", e.reason)
                return

            except Exception:
                logging.error("Unrecoverable encountered at url: %s", url)
                raise


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--log', metavar='-l', type=str,
                        help='logging level', default='info')
    parser.add_argument('--config', metavar='-c', required=True,
                        type=str, help='config file')
    parser.add_argument('--timeout', metavar='-t', required=False, type=int,
                        help='how many seconds the crawler should be running',
                        default=False)
    args = parser.parse_args()

    level = getattr(logging, args.log.upper())
    logging.basicConfig(level=level)

    crawler = Crawler()
    crawler.load_config_file(args.config)

    if args.timeout:
        crawler.set_option("timeout", args.timeout)

    crawler.crawl()


if __name__ == "__main__":
    main()
