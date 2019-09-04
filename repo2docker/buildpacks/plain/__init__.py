import textwrap
import jinja2
import tarfile
import io
import os
import re
import logging
import sys

from ..base import BuildPack

from traitlets import Dict

TEMPLATE = r"""
FROM buildpack-deps:bionic

{% if set_up_apt -%}
ENV DEBIAN_FRONTEND=noninteractive
{% endif -%}

{% if set_up_locales -%}
RUN apt-get -qq update && \
    apt-get -qq install --yes --no-install-recommends locales > /dev/null && \
    apt-get -qq purge && \
    apt-get -qq clean && \
    rm -rf /var/lib/apt/lists/*
RUN echo "en_US.UTF-8 UTF-8" > /etc/locale.gen && \
    locale-gen
ENV LC_ALL en_US.UTF-8
ENV LANG en_US.UTF-8
ENV LANGUAGE en_US.UTF-8
{% endif -%}

{% if base_packages -%}
RUN apt-get -qq update && \
    apt-get -qq install --yes --no-install-recommends \
       {% for package in base_packages -%}
       {{ package }} \
       {% endfor -%}
    > /dev/null && \
    apt-get -qq purge && \
    apt-get -qq clean && \
    rm -rf /var/lib/apt/lists/*
{% endif -%}

{% if packages -%}
RUN apt-get -qq update && \
    apt-get -qq install --yes \
       {% for package in packages -%}
       {{ package }} \
       {% endfor -%}
    > /dev/null && \
    apt-get -qq purge && \
    apt-get -qq clean && \
    rm -rf /var/lib/apt/lists/*
{% endif -%}

{% if build_env -%}
{% for item in build_env -%}
ENV {{item[0]}} {{item[1]}}
{% endfor -%}
{% endif -%}

{% if path -%}
ENV PATH {{ ':'.join(path) }}:${PATH}
{% endif -%}

{% if build_script_files -%}
{% for src, dst in build_script_files|dictsort %}
COPY {{ src }} {{ dst }}
{% endfor -%}
{% endif -%}

{% for sd in build_script_directives -%}
{{sd}}
{% endfor %}

{% if workdir -%}
WORKDIR workdir
{% endif -%}

{% if env -%}
{% for item in env -%}
ENV {{item[0]}} {{item[1]}}
{% endfor -%}
{% endif -%}

{% if preassemble_script_files -%}
{% for src, dst in preassemble_script_files|dictsort %}
COPY src/{{ src }} ${REPO_DIR}/{{ dst }}
{% endfor -%}
{% endif -%}

{% if preassemble_script_directives -%}
USER root
RUN chown -R ${NB_USER}:${NB_USER} ${REPO_DIR}
{% endif -%}

{% for sd in preassemble_script_directives -%}
{{ sd }}
{% endfor %}

{% for sd in assemble_script_directives -%}
{{ sd }}
{% endfor %}

{% for k, v in labels|dictsort %}
LABEL {{k}}="{{v}}"
{%- endfor %}

{% if post_build_scripts -%}
{% for s in post_build_scripts -%}
RUN chmod +x {{ s }}
RUN ./{{ s }}
{% endfor %}
{% endif -%}

{% if start_script is not none -%}
RUN chmod +x "{{ start_script }}"
ENV R2D_ENTRYPOINT "{{ start_script }}"
{% endif -%}

{% if entrypoint -%}
ENTRYPOINT [" {{entrypoint }}"]
{% endif %}

{% if cmd -%}
CMD ["{{ cmd }}"]
{% endif %}

"""


class PlainBuildPack(BuildPack):
    """
    A composable BuildPack.

    Specifically used for creating Dockerfiles NOT used with repo2docker.

    Things that are kept constant:
     - base image
     - user is always root

    """

    def __init__(self, r2d):
        super().__init__()

        self.plain = False
        if hasattr(r2d, 'plain'):
            self.plain = r2d.plain

    def get_packages(self):
        return set()
        #return {
        #    # Utils!
        #    "less",
        #    "nodejs",
        #    "unzip",
        #}

    def get_preassemble_scripts(self):
        scripts = []
        try:
            with open(self.binder_path("apt.txt")) as f:
                extra_apt_packages = []
                for l in f:
                    package = l.partition("#")[0].strip()
                    if not package:
                        continue
                    # Validate that this is, indeed, just a list of packages
                    # We're doing shell injection around here, gotta be careful.
                    # FIXME: Add support for specifying version numbers
                    if not re.match(r"^[a-z0-9.+-]+", package):
                        raise ValueError(
                            "Found invalid package name {} in "
                            "apt.txt".format(package)
                        )
                    extra_apt_packages.append(package)

            scripts.append(
                (
                    "root",
                    # This apt-get install is *not* quiet, since users explicitly asked for this
                    r"""
                apt-get -qq update && \
                apt-get install --yes --no-install-recommends {} && \
                apt-get -qq purge && \
                apt-get -qq clean && \
                rm -rf /var/lib/apt/lists/*
                """.format(
                        " ".join(sorted(extra_apt_packages))
                    ),
                )
            )

        except FileNotFoundError:
            pass

        return scripts

    def get_post_build_scripts(self):
        post_build = self.binder_path("postBuild")
        if os.path.exists(post_build):
            return [post_build]
        return []

    def get_start_script(self):
        start = self.binder_path("start")
        if os.path.exists(start):
            # Return an absolute path to start
            # This is important when built container images start with
            # a working directory that is different from ${REPO_DIR}
            # This isn't a problem with anything else, since start is
            # the only path evaluated at container start time rather than build time
            return os.path.join("${REPO_DIR}", start)
        return None

    def detect(self):
        """
        Check if current repo should be built with the plain base Build pack
        """
        return self.plain

    def render(self):
        """
        Render BuildPack into Dockerfile
        """
        t = jinja2.Template(TEMPLATE)

        build_script_directives = []
        last_user = "root"
        for user, script in self.get_build_scripts():
            if last_user != user:
                build_script_directives.append("USER {}".format(user))
                last_user = user
            build_script_directives.append(
                "RUN {}".format(textwrap.dedent(script.strip("\n")))
            )

        assemble_script_directives = []
        last_user = "root"
        for user, script in self.get_assemble_scripts():
            if last_user != user:
                assemble_script_directives.append("USER {}".format(user))
                last_user = user
            assemble_script_directives.append(
                "RUN {}".format(textwrap.dedent(script.strip("\n")))
            )

        preassemble_script_directives = []
        last_user = "root"
        for user, script in self.get_preassemble_scripts():
            if last_user != user:
                preassemble_script_directives.append("USER {}".format(user))
                last_user = user
            preassemble_script_directives.append(
                "RUN {}".format(textwrap.dedent(script.strip("\n")))
            )

        return t.render(
            packages=sorted(self.get_packages()),
            path=self.get_path(),
            build_env=self.get_build_env(),
            env=self.get_env(),
            labels=self.get_labels(),
            build_script_directives=build_script_directives,
            preassemble_script_files=self.get_preassemble_script_files(),
            preassemble_script_directives=preassemble_script_directives,
            assemble_script_directives=assemble_script_directives,
            build_script_files=self.get_build_script_files(),
            post_build_scripts=self.get_post_build_scripts(),
            start_script=self.get_start_script(),
            cmd = "my_command",
            entrypoint = "my_entrypoint"
        )

    def build(
        self,
        client,
        image_spec,
        memory_limit,
        build_args,
        cache_from,
        extra_build_kwargs,
    ):
        tarf = io.BytesIO()
        tar = tarfile.open(fileobj=tarf, mode="w")
        dockerfile_tarinfo = tarfile.TarInfo("Dockerfile")
        dockerfile = self.render().encode("utf-8")
        dockerfile_tarinfo.size = len(dockerfile)

        tar.addfile(dockerfile_tarinfo, io.BytesIO(dockerfile))

        def _filter_tar(tar):
            # We need to unset these for build_script_files we copy into tar
            # Otherwise they seem to vary each time, preventing effective use
            # of the cache!
            # https://github.com/docker/docker-py/pull/1582 is related
            tar.uname = ""
            tar.gname = ""
            tar.uid = int(build_args.get("NB_UID", 1000))
            tar.gid = int(build_args.get("NB_UID", 1000))
            return tar

        for src in sorted(self.get_build_script_files()):
            src_parts = src.split("/")
            src_path = os.path.join(os.path.dirname(__file__), *src_parts)
            tar.add(src_path, src, filter=_filter_tar)

        tar.add(".", "src/", filter=_filter_tar)

        tar.close()
        tarf.seek(0)

        # If you work on this bit of code check the corresponding code in
        # buildpacks/docker.py where it is duplicated
        if not isinstance(memory_limit, int):
            raise ValueError(
                "The memory limit has to be specified as an"
                "integer but is '{}'".format(type(memory_limit))
            )
        limits = {}
        if memory_limit:
            # We want to always disable swap. Docker expects `memswap` to
            # be total allowable memory, *including* swap - while `memory`
            # points to non-swap memory. We set both values to the same so
            # we use no swap.
            limits = {"memory": memory_limit, "memswap": memory_limit}

        build_kwargs = dict(
            fileobj=tarf,
            tag=image_spec,
            custom_context=True,
            buildargs=build_args,
            decode=True,
            forcerm=True,
            rm=True,
            container_limits=limits,
            cache_from=cache_from,
        )

        build_kwargs.update(extra_build_kwargs)

        for line in client.build(**build_kwargs):
            yield line
