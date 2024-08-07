#!/usr/bin/env python
import pandas as pd
import argparse
import re
import asyncio
import logging
import time
from twscrape import API, gather
import random

# Set up logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

async def random_sleep():
    await asyncio.sleep(random.uniform(1, 3)) 

async def retry_with_backoff(api_call, retries=3):
    delay = 1
    for attempt in range(retries):
        try:
            return await api_call()
        except Exception as e:
            if attempt < retries - 1:
                time.sleep(delay)
                delay *= 2  # Exponential backoff
            else:
                raise e
async def scrap_tweets(credentials_file, users_path, text_limit):
    logging.info("Starting the scraping process")

    with open(credentials_file, 'r') as file:
        credentials = [line.strip().split(':') for line in file]

    with open(users_path, 'r') as file:
        data = file.read()
    match = re.search(r'users\s*=\s*(\[.*\])', data, re.DOTALL)
    users_list = eval(match.group(1)) if match else []

    df = pd.DataFrame(columns=[
        'tweet_id', 'user', 'created_at', 'post_text', 'lang', 'ViewCount', 'quoteCount', 'likeCount', 'replyCount', 
        'retweetCount', 'bookmarkCount', 'is_retweet', 'is_quote', 'is_reply', 'reply_to_id', 'reply_to_user', 
        'reply_to_text', 'original_tweet_id', 'original_tweet_user', 'original_tweet_text', 'is_mutual_followership'
    ])

    api = API()

    async def add_account_if_not_exists(account):
        try:
            await api.pool.add_account(account[0], account[1], account[2], account[3])
            logging.info(f"Added account {account[0]}")
        except Exception as e:
            logging.warning(f"Warning: {e}")

    logging.info("Adding accounts")
    for credential in credentials:
        await add_account_if_not_exists(credential)
        await random_sleep()  # Add a random delay after adding each account

    logging.info("Logging in all accounts")
    await api.pool.login_all()

    logging.info("Starting to scrape users")
    processed_tweet_ids = set()  # Track processed tweet IDs

    for user in users_list[:]:
        index = users_list.index(user) + 1
        total_users = len(users_list)
        logging.info(f"Scraping tweets for user: {user} ({index}/{total_users}, {index/total_users*100:.2f}% complete)")
        user_info = await api.user_by_login(user)
        if user_info is None:
            logging.warning(f"User {user} not found")
            continue
        user_id = user_info.dict()['id']

        try:
            tweets = await gather(api.user_tweets(user_id, limit=text_limit))
            retweets_and_replies = await gather(api.user_tweets_and_replies(user_id, limit=text_limit))
        except Exception as e:
            logging.warning(f"Rate limit hit, waiting before retrying: {e}")
            time.sleep(900)  # wait for 15 minutes
            tweets = await gather(api.user_tweets(user_id, limit=text_limit))
            retweets_and_replies = await gather(api.user_tweets_and_replies(user_id, limit=text_limit))

        followings = await gather(api.following(user_id, limit=text_limit))
        followers = await gather(api.followers(user_id, limit=text_limit))

        followers_ids = {follower.dict()['id'] for follower in followers}
        following_ids = {following.dict()['id'] for following in followings}

        tweet_dict = {tweet.dict()['id']: tweet.dict() for tweet in tweets + retweets_and_replies}

        for tweet in tweets:
            tweet_data = tweet.dict()
            logging.debug(f"Tweet data: {tweet_data}")  # Detailed logging for debugging

            # Skip tweets from users not in the users_list
            if tweet_data['user']['username'] not in users_list:
                logging.info(f"Skipping tweet from user: {tweet_data['user']['username']}")
                continue

            tweet_id = tweet_data['id']
            if tweet_id in processed_tweet_ids:
                logging.info(f"Duplicate tweet ID: {tweet_id}, skipping.")
                continue

            processed_tweet_ids.add(tweet_id)  # Mark this tweet ID as processed

            mutuality = 'Mutual' if tweet_data['user']['username'] in followers_ids and tweet_data['user']['id'] in following_ids else 'Not Mutual'

            is_retweet = 'retweetedTweet' in tweet_data and tweet_data['retweetedTweet'] is not None
            is_quote = 'quotedTweet' in tweet_data and tweet_data['quotedTweet'] is not None
            is_reply = tweet_data.get('inReplyToTweetId') is not None

            reply_to_id = None
            reply_to_user = None
            reply_to_text = None
            original_tweet_id = None
            original_tweet_user = None
            original_tweet_text = None

            if is_retweet:
                original_tweet_user = tweet_data['retweetedTweet']['user']['username']
                original_tweet_text = tweet_data['retweetedTweet']['rawContent']
                original_tweet_id = tweet_data['retweetedTweet']['id']
            elif is_quote:
                original_tweet_user = tweet_data['quotedTweet']['user']['username']
                original_tweet_text = tweet_data['quotedTweet']['rawContent']
                original_tweet_id = tweet_data['quotedTweet']['id']
            elif is_reply and tweet_data['inReplyToTweetId'] is not None:
                reply_to_id = tweet_data['inReplyToTweetId']
                reply_to_user = tweet_data['inReplyToUser']['username'] if tweet_data['inReplyToUser'] is not None else None
                original_tweet_id = reply_to_id
                original_tweet_user = reply_to_user
                if original_tweet_id in tweet_dict:
                    original_tweet_text = tweet_dict[original_tweet_id]['rawContent']
                else:
                    original_tweet_text = None  # or fetch the original tweet text if necessary

            logging.info(f"Tweet ID: {tweet_id} - is_retweet: {is_retweet}, is_quote: {is_quote}, is_reply: {is_reply}")  # Log tweet types

            new_row = pd.DataFrame([{
                'tweet_id': str(tweet_id),
                'user': tweet_data['user']['username'],
                'created_at': tweet_data['date'].strftime("%Y/%m/%d %H:%M:%S"),
                'post_text': tweet_data['rawContent'],
                'lang': tweet_data['lang'],
                'ViewCount': tweet_data.get('viewCount'),
                'quoteCount': tweet_data.get('quoteCount'),
                'likeCount': tweet_data.get('likeCount'),
                'replyCount': tweet_data.get('replyCount'),
                'retweetCount': tweet_data.get('retweetCount'),
                'bookmarkCount': tweet_data.get('bookmarkedCount'),
                'is_retweet': is_retweet,
                'is_quote': is_quote,
                'is_reply': is_reply,
                'reply_to_id': str(reply_to_id),
                'reply_to_user': reply_to_user,
                'reply_to_text': reply_to_text,
                'original_tweet_id': str(original_tweet_id),
                'original_tweet_user': original_tweet_user,
                'original_tweet_text': original_tweet_text,
                'is_mutual_followership': mutuality
            }])

            df = pd.concat([df, new_row], ignore_index=True)
            logging.info(f"Processed tweet ID: {tweet_id} for user: {user}")

        for tweet in retweets_and_replies: 
            tweet_data = tweet.dict()
            logging.debug(f"retweets_and_replies data: {tweet_data}")  # Detailed logging for debugging

            # Skip tweets from users not in the users_list
            if tweet_data['user']['username'] not in users_list:
                logging.info(f"Skipping tweet from user: {tweet_data['user']['username']}")
                continue

            tweet_id = tweet_data['id']
            if tweet_id in processed_tweet_ids:
                logging.info(f"Duplicate tweet ID: {tweet_id}, skipping.")
                continue

            processed_tweet_ids.add(tweet_id)  # Mark this tweet ID as processed

            is_retweet = 'retweetedTweet' in tweet_data and tweet_data['retweetedTweet'] is not None
            is_quote = 'quotedTweet' in tweet_data and tweet_data['quotedTweet'] is not None
            is_reply = tweet_data.get('inReplyToTweetId') is not None

            reply_to_id = None
            reply_to_user = None
            reply_to_text = None
            original_tweet_id = None
            original_tweet_user = None
            original_tweet_text = None

            if is_retweet:
                original_tweet_user = tweet_data['retweetedTweet']['user']['username']
                original_tweet_text = tweet_data['retweetedTweet']['rawContent']
                original_tweet_id = tweet_data['retweetedTweet']['id']
            elif is_quote:
                original_tweet_user = tweet_data['quotedTweet']['user']['username']
                original_tweet_text = tweet_data['quotedTweet']['rawContent']
                original_tweet_id = tweet_data['quotedTweet']['id']
            elif is_reply and tweet_data['inReplyToTweetId'] is not None:
                reply_to_id = tweet_data['inReplyToTweetId']
                reply_to_user = tweet_data['inReplyToUser']['username'] if tweet_data['inReplyToUser'] is not None else None
                original_tweet_id = reply_to_id
                original_tweet_user = reply_to_user
                if original_tweet_id in tweet_dict:
                    original_tweet_text = tweet_dict[original_tweet_id]['rawContent']
                else:
                    original_tweet_text = None  # or fetch the original tweet text if necessary

            followers_user = {follower.dict()['username'] for follower in followers}
            following_user = {following.dict()['username'] for following in followings}

            mutuality = 'Mutual' if reply_to_user in followers_user and reply_to_user in following_user else 'Not Mutual'
            logging.info(f"Mutuality: {mutuality}")

            new_row = pd.DataFrame([{
                'tweet_id': str(tweet_id),
                'user': tweet_data['user']['username'],
                'created_at': tweet_data['date'].strftime("%Y/%m/%d %H:%M:%S"),
                'post_text': tweet_data['rawContent'],
                'lang': tweet_data['lang'],
                'ViewCount': tweet_data.get('viewCount'),
                'quoteCount': tweet_data.get('quoteCount'),
                'likeCount': tweet_data.get('likeCount'),
                'replyCount': tweet_data.get('replyCount'),
                'retweetCount': tweet_data.get('retweetCount'),
                'bookmarkCount': tweet_data.get('bookmarkedCount'),
                'is_retweet': is_retweet,
                'is_quote': is_quote,
                'is_reply': is_reply,
                'reply_to_id': str(reply_to_id),
                'reply_to_user': reply_to_user,
                'reply_to_text': reply_to_text,
                'original_tweet_id': str(original_tweet_id),
                'original_tweet_user': original_tweet_user,
                'original_tweet_text': original_tweet_text,
                'is_mutual_followership': mutuality
            }])

            df = pd.concat([df, new_row], ignore_index=True)
            logging.info(f"Processed tweet ID: {tweet_id} for user: {user}")

        await random_sleep()  # Add a random delay after processing each user

    logging.info("Scraping process completed")
    return df

async def main(credentials_file, users_path, text_limit, path_to_save):
    logging.info("Main function started")
    df = await scrap_tweets(credentials_file, users_path, int(text_limit))
    df['created_at'] = pd.to_datetime(df['created_at'])
    df = df.sort_values(by=['user', 'created_at'])
    df.to_csv(path_to_save, index=False)
    logging.info(f"Data saved to {path_to_save}")

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument('credentials', metavar='FILE', type=str, help='Path to the credentials text file')
    parser.add_argument('users_path', metavar='FILE', type=str, help='Path to the users list file')
    parser.add_argument('text_limit', metavar='LIMIT', type=int, help='Text limit')
    parser.add_argument('path_to_save', metavar='FILE', type=str, help='Path to save the generated scraping file')

    args = parser.parse_args()

    asyncio.run(main(args.credentials, args.users_path, args.text_limit, args.path_to_save))
