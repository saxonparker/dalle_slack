"""Generate Dalle images for slack. Designed to run as either a command-line
application or as an AWS Lambda pair."""

import argparse
import json
import os
import random
import string
import sys
import traceback
import typing
import shutil
import urllib.parse

import boto3
import openai
import requests
from PIL import Image


def generate_image(prompt):
    """Generate the dalle image"""
    openai.organization = os.environ['OPENAI_ORGANIZATION']
    openai.api_key = os.environ['OPENAI_API_KEY']
    response = openai.images.generate(prompt=prompt, n=1, size="1024x1024", model="dall-e-3", quality="hd")
    image_url = response.data[0].url
    return image_url


def validate_prompt(prompt):
    """Validate's a prompt using OpenAI's Moderation API"""
    openai.organization = os.environ['OPENAI_ORGANIZATION']
    openai.api_key = os.environ['OPENAI_API_KEY']
    response = openai.Moderation.create(input=prompt)
    results = response['results'][0]
    return results


####################################################################################################
# Prompt Manipulation Code                                                                         #
####################################################################################################

class Manipulation(typing.NamedTuple):
    """
    Manipulates source prompts to be something a little more fun. This system is meant as a prank to
    make a friend wonder why so many of his prompts come out with corn-themed results.
    """

    source: str
    """
    The source format string for the manipulation. It must contain a ``{prompt}`` and ``{choice}``
    text, which are the original source prompt and a random choice from the ``potentials``. For
    example, source ``"{choice} of {prompt}"`` and potentials ``["oil painting", "watercolor"]``
    would lead to results like ``"watercolor of Darth Vader trying to drink a milkshake"``.
    """

    potentials: typing.Sequence[str]

    def alter(self, prompt: str) -> str:
        """Alter the ``prompt`` and return the result."""
        return str.format(self.source, prompt=prompt, choice=random.choice(self.potentials))


def get_user_specific_manipulations(user: str) -> typing.Sequence[Manipulation]:
    """
    Get a list of manipulations for the given ``user``. If the user has no manipulations, this
    returns an empty tuple.
    """
    if user == 'matthew.moskowitz9':
        basic_corn = ("corn",
                      "corn cob",
                      "corn kernel",
                      "corn dog",
                      "creamed corn",
                      "corn puffs",
                      "popcorn",
                      )
        return (Manipulation("{prompt} with {choice}", basic_corn),
                Manipulation("{prompt} on a {choice}", basic_corn),
                Manipulation("{prompt} in a {choice}", ("corn field", "bowl of creamed corn")),
                Manipulation("{prompt} holding {choice}", basic_corn),
                Manipulation("a mural made of {choice}, depicting {prompt}",
                             ("corn kernels", "corn puffs", "popcorn")
                             ),
                Manipulation("a {choice} thinking about {prompt}", ("corn cob man", "corn dog",)),
                Manipulation("a corn-based {choice} of {prompt}", ("NFT", "cryptocurrency")),
                )

    return tuple()


def sanitize_prompt(prompt: str, user: str) -> str:
    """Alter the input prompt with user-specific manipulations.

    Returns
    -------
    The new prompt. If the prompt was unmanipulated, the input ``prompt`` is returned directly.
    """
    manips = get_user_specific_manipulations(user)
    if len(manips) == 0:
        return prompt
    return random.choice(manips).alter(prompt)


def upload_to_s3(prompt, url):
    """Upload image to s3"""
    raw_local_file = '/tmp/local_raw.jpeg'
    comp_local_file = '/tmp/local_compresses.jpeg'
    s3_client = boto3.client('s3')
    image_response = requests.get(url, stream=True, timeout=10000).raw
    with open(raw_local_file,'wb') as file:
        shutil.copyfileobj(image_response, file)

    image = Image.open(raw_local_file)

    image.save(comp_local_file,
                 "JPEG",
                 optimize = True,
                 quality = 50)

    rand_tag = ''.join(random.choices(string.ascii_uppercase +
                             string.digits, k=10))
    final_file = f'{prompt[:512].replace(" ", "_")}_{rand_tag}.jpeg'
    encoded = urllib.parse.quote(final_file)
    print(f'Final file {final_file}, Encoded url: {encoded}')
    s3_client.upload_file(comp_local_file, 'dallepics', f'dalle/{final_file}')
    uploaded_url = f'https://d2jagmvo7k5q5j.cloudfront.net/dalle/{encoded}'
    return uploaded_url

def parse_args(input_str):
    """Parse any flags and directives

    Returns
    -------
    The prompt to generate and the text to display.
    """
    opts = input_str.split(" ")
    parser = argparse.ArgumentParser(description="Generate chatgpt text")
    parser.add_argument("prompt", nargs="+", help="The text of the prompt")

    args = parser.parse_args(opts)
    text = " ".join(args.prompt)

    split_text = text.split("[")
    display_text = split_text[0].strip()
    if len(split_text) > 1:
        right_split = split_text[1].split("]")
        if len(right_split) > 1:
            display_text += right_split[1]

    prompt_text = text.replace("[", "").replace("]", "")

    return (display_text, prompt_text)


def dalle(event, _):
    """Entry point for the lambda that actually generates the image."""
    response_url = None # <- Setting here to prevent NameError in except case
    try:
    # pylint: disable=broad-except
        # Process the SNS message.
        print(f"SNS MESSAGE: {event['Records'][0]['Sns']['Message']}")
        message = json.loads(event['Records'][0]['Sns']['Message'])
        response_url = message['response_url']
        input_str = message['prompt']
        user = message['user']

        (display, prompt) = parse_args(input_str)
        sanitized_prompt = sanitize_prompt(prompt, user)

        # Don't actually validate the prompt because it gives different results than when dalle
        # actually flags.
        # Leave the code in for now in case this changes in the future.
        #
        # print('VALIDATE PROMPT: ' + prompt)
        # validation = validate_prompt(prompt)
        # if validation['flagged']:
        #     requests.post(response_url, data=json.dumps({'text': validation}), timeout=10000)
        #     print('VALIDATION FAILED')
        #     return

        # Process the command.
        print('GENERATE IMAGE: ' + sanitized_prompt)
        image = generate_image(sanitized_prompt)

        print('GENERATE IMAGE COMPLETE' + image)

        print('UPLOADING TO IMGUR')

        uploaded = upload_to_s3(sanitized_prompt, image)

        print('UPLOAD URL ' + uploaded)

        requests.post(response_url,
                      data=json.dumps({"response_type": "in_channel", "attachments": [
            {
                "fallback": prompt,
                "text": f'{user} generated: "{display}"',
                "image_url": uploaded,
            }
            ]}), timeout=10000)

    except Exception as exc:
        print('COMMAND ERROR: ' + str(exc))
        traceback.print_exc()
        requests.post(response_url, data=json.dumps({'text': str(exc)}), timeout=10000)
    # pylint: enable=broad-except

def main():
    """Process the command given on the command line."""
    prompt = ' '.join(sys.argv[1:])
    image = generate_image(prompt)
    print(image)
    uploaded = upload_to_s3(prompt, image)
    print(uploaded)

if __name__ == '__main__':
    main()
