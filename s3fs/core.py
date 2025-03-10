# -*- coding: utf-8 -*-
import asyncio
import errno
import logging
import os
import socket
from typing import Tuple, Optional
import weakref

from fsspec.spec import AbstractBufferedFile
from fsspec.utils import infer_storage_options, tokenize, setup_logging as setup_logger
from fsspec.asyn import AsyncFileSystem, sync, sync_wrapper, FSTimeoutError

import aiobotocore
import botocore
import aiobotocore.session
from aiobotocore.config import AioConfig
from botocore.exceptions import ClientError, ParamValidationError

from s3fs.errors import translate_boto_error
from s3fs.utils import S3BucketRegionCache, ParamKwargsHelper, _get_brange, FileExpired


logger = logging.getLogger("s3fs")


def setup_logging(level=None):

    setup_logger(logger=logger, level=(level or os.environ["S3FS_LOGGING_LEVEL"]))


if "S3FS_LOGGING_LEVEL" in os.environ:
    setup_logging()


MANAGED_COPY_THRESHOLD = 5 * 2 ** 30
S3_RETRYABLE_ERRORS = (socket.timeout,)

_VALID_FILE_MODES = {"r", "w", "a", "rb", "wb", "ab"}


key_acls = {
    "private",
    "public-read",
    "public-read-write",
    "authenticated-read",
    "aws-exec-read",
    "bucket-owner-read",
    "bucket-owner-full-control",
}
buck_acls = {"private", "public-read", "public-read-write", "authenticated-read"}


def version_id_kw(version_id):
    """Helper to make versionId kwargs.

    Not all boto3 methods accept a None / empty versionId so dictionary expansion solves
    that problem.
    """
    if version_id:
        return {"VersionId": version_id}
    else:
        return {}


def _coalesce_version_id(*args):
    """Helper to coalesce a list of version_ids down to one"""
    version_ids = set(args)
    if None in version_ids:
        version_ids.remove(None)
    if len(version_ids) > 1:
        raise ValueError(
            "Cannot coalesce version_ids where more than one are defined,"
            " {}".format(version_ids)
        )
    elif len(version_ids) == 0:
        return None
    else:
        return version_ids.pop()


class S3FileSystem(AsyncFileSystem):
    """
    Access S3 as if it were a file system.

    This exposes a filesystem-like API (ls, cp, open, etc.) on top of S3
    storage.

    Provide credentials either explicitly (``key=``, ``secret=``) or depend
    on boto's credential methods. See botocore documentation for more
    information. If no credentials are available, use ``anon=True``.

    Parameters
    ----------
    anon : bool (False)
        Whether to use anonymous connection (public buckets only). If False,
        uses the key/secret given, or boto's credential resolver (client_kwargs,
        environment, variables, config files, EC2 IAM server, in that order)
    key : string (None)
        If not anonymous, use this access key ID, if specified
    secret : string (None)
        If not anonymous, use this secret access key, if specified
    token : string (None)
        If not anonymous, use this security token, if specified
    use_ssl : bool (True)
        Whether to use SSL in connections to S3; may be faster without, but
        insecure. If ``use_ssl`` is also set in ``client_kwargs``,
        the value set in ``client_kwargs`` will take priority.
    s3_additional_kwargs : dict of parameters that are used when calling s3 api
        methods. Typically used for things like "ServerSideEncryption".
    client_kwargs : dict of parameters for the botocore client
    requester_pays : bool (False)
        If RequesterPays buckets are supported.
    default_block_size: int (None)
        If given, the default block size value used for ``open()``, if no
        specific value is given at all time. The built-in default is 5MB.
    default_fill_cache : Bool (True)
        Whether to use cache filling with open by default. Refer to
        ``S3File.open``.
    default_cache_type : string ('bytes')
        If given, the default cache_type value used for ``open()``. Set to "none"
        if no caching is desired. See fsspec's documentation for other available
        cache_type values. Default cache_type is 'bytes'.
    version_aware : bool (False)
        Whether to support bucket versioning.  If enable this will require the
        user to have the necessary IAM permissions for dealing with versioned
        objects.
    cache_regions : bool (False)
        Whether to cache bucket regions or not. Whenever a new bucket is used,
        it will first find out which region it belongs and then use the client
        for that region.
    config_kwargs : dict of parameters passed to ``botocore.client.Config``
    kwargs : other parameters for core session
    session : aiobotocore AioSession object to be used for all connections.
         This session will be used inplace of creating a new session inside S3FileSystem.
         For example: aiobotocore.AioSession(profile='test_user')

    The following parameters are passed on to fsspec:

    skip_instance_cache: to control reuse of instances
    use_listings_cache, listings_expiry_time, max_paths: to control reuse of directory listings

    Examples
    --------
    >>> s3 = S3FileSystem(anon=False)  # doctest: +SKIP
    >>> s3.ls('my-bucket/')  # doctest: +SKIP
    ['my-file.txt']

    >>> with s3.open('my-bucket/my-file.txt', mode='rb') as f:  # doctest: +SKIP
    ...     print(f.read())  # doctest: +SKIP
    b'Hello, world!'
    """

    root_marker = ""
    connect_timeout = 5
    retries = 5
    read_timeout = 15
    default_block_size = 5 * 2 ** 20
    protocol = ["s3", "s3a"]
    _extra_tokenize_attributes = ("default_block_size",)

    def __init__(
        self,
        anon=False,
        key=None,
        secret=None,
        token=None,
        use_ssl=True,
        client_kwargs=None,
        requester_pays=False,
        default_block_size=None,
        default_fill_cache=True,
        default_cache_type="bytes",
        version_aware=False,
        config_kwargs=None,
        s3_additional_kwargs=None,
        session=None,
        username=None,
        password=None,
        cache_regions=False,
        asynchronous=False,
        loop=None,
        **kwargs
    ):
        if key and username:
            raise KeyError("Supply either key or username, not both")
        if secret and password:
            raise KeyError("Supply secret or password, not both")
        if username:
            key = username
        if password:
            secret = password

        self.anon = anon
        self.key = key
        self.secret = secret
        self.token = token
        self.kwargs = kwargs
        super_kwargs = {
            k: kwargs.pop(k)
            for k in ["use_listings_cache", "listings_expiry_time", "max_paths"]
            if k in kwargs
        }  # passed to fsspec superclass
        super().__init__(loop=loop, asynchronous=asynchronous, **super_kwargs)

        self.default_block_size = default_block_size or self.default_block_size
        self.default_fill_cache = default_fill_cache
        self.default_cache_type = default_cache_type
        self.version_aware = version_aware
        self.client_kwargs = client_kwargs or {}
        self.config_kwargs = config_kwargs or {}
        self.req_kw = {"RequestPayer": "requester"} if requester_pays else {}
        self.s3_additional_kwargs = s3_additional_kwargs or {}
        self.use_ssl = use_ssl
        self.cache_regions = cache_regions
        self._s3 = None
        self.session = session

    @property
    def s3(self):
        if self._s3 is None:
            if self.asynchronous:
                raise RuntimeError("please await ``.set_session`` before anything else")
            self.connect()
        return self._s3

    def _filter_kwargs(self, s3_method, kwargs):
        return self._kwargs_helper.filter_dict(s3_method.__name__, kwargs)

    async def get_s3(self, bucket=None):
        if self.cache_regions and bucket is not None:
            return await self._s3creator.get_bucket_client(bucket)
        else:
            return self._s3

    async def _call_s3(self, method, *akwarglist, **kwargs):
        await self.set_session()
        s3 = await self.get_s3(kwargs.get("Bucket"))
        method = getattr(s3, method)
        kw2 = kwargs.copy()
        kw2.pop("Body", None)
        logger.debug("CALL: %s - %s - %s", method.__name__, akwarglist, kw2)
        additional_kwargs = self._get_s3_method_kwargs(method, *akwarglist, **kwargs)
        for i in range(self.retries):
            try:
                out = await method(**additional_kwargs)
                return out
            except S3_RETRYABLE_ERRORS as e:
                logger.debug("Retryable error: %s", e)
                err = e
                await asyncio.sleep(min(1.7 ** i * 0.1, 15))
            except Exception as e:
                logger.debug("Nonretryable error: %s", e)
                err = e
                break
        if "'coroutine'" in str(err):
            # aiobotocore internal error - fetch original botocore error
            tb = err.__traceback__
            while tb.tb_next:
                tb = tb.tb_next
            try:
                await tb.tb_frame.f_locals["response"]
            except Exception as e:
                err = e
        raise translate_boto_error(err)

    call_s3 = sync_wrapper(_call_s3)

    def _get_s3_method_kwargs(self, method, *akwarglist, **kwargs):
        additional_kwargs = self.s3_additional_kwargs.copy()
        for akwargs in akwarglist:
            additional_kwargs.update(akwargs)
        # Add the normal kwargs in
        additional_kwargs.update(kwargs)
        # filter all kwargs
        return self._filter_kwargs(method, additional_kwargs)

    @staticmethod
    def _get_kwargs_from_urls(urlpath):
        """
        When we have a urlpath that contains a ?versionId=

        Assume that we want to use version_aware mode for
        the filesystem.
        """
        url_storage_opts = infer_storage_options(urlpath)
        url_query = url_storage_opts.get("url_query")
        out = {}
        if url_query is not None:
            from urllib.parse import parse_qs

            parsed = parse_qs(url_query)
            if "versionId" in parsed:
                out["version_aware"] = True
        return out

    def split_path(self, path) -> Tuple[str, str, Optional[str]]:
        """
        Normalise S3 path string into bucket and key.

        Parameters
        ----------
        path : string
            Input path, like `s3://mybucket/path/to/file`

        Examples
        --------
        >>> split_path("s3://mybucket/path/to/file")
        ['mybucket', 'path/to/file', None]

        >>> split_path("s3://mybucket/path/to/versioned_file?versionId=some_version_id")
        ['mybucket', 'path/to/versioned_file', 'some_version_id']
        """
        path = self._strip_protocol(path)
        path = path.lstrip("/")
        if "/" not in path:
            return path, "", None
        else:
            bucket, keypart = path.split("/", 1)
            key, _, version_id = keypart.partition("?versionId=")
            return (
                bucket,
                key,
                version_id if self.version_aware and version_id else None,
            )

    def _prepare_config_kwargs(self):
        config_kwargs = self.config_kwargs.copy()
        if "connect_timeout" not in config_kwargs.keys():
            config_kwargs["connect_timeout"] = self.connect_timeout
        if "read_timeout" not in config_kwargs.keys():
            config_kwargs["read_timeout"] = self.read_timeout
        return config_kwargs

    async def set_session(self, refresh=False, kwargs={}):
        """Establish S3 connection object.
        Returns
        -------
        Session to be closed later with await .close()
        """
        if self._s3 is not None and not refresh:
            return self._s3
        logger.debug("Setting up s3fs instance")

        client_kwargs = self.client_kwargs.copy()
        init_kwargs = dict(
            aws_access_key_id=self.key,
            aws_secret_access_key=self.secret,
            aws_session_token=self.token,
        )
        init_kwargs = {
            key: value
            for key, value in init_kwargs.items()
            if value is not None and value != client_kwargs.get(key)
        }
        if "use_ssl" not in client_kwargs.keys():
            init_kwargs["use_ssl"] = self.use_ssl
        config_kwargs = self._prepare_config_kwargs()
        if self.anon:
            from botocore import UNSIGNED

            drop_keys = {
                "aws_access_key_id",
                "aws_secret_access_key",
                "aws_session_token",
            }
            init_kwargs = {
                key: value for key, value in init_kwargs.items() if key not in drop_keys
            }
            client_kwargs = {
                key: value
                for key, value in client_kwargs.items()
                if key not in drop_keys
            }
            config_kwargs["signature_version"] = UNSIGNED

        conf = AioConfig(**config_kwargs)
        if self.session is None:
            self.session = aiobotocore.AioSession(**self.kwargs)

        for parameters in (config_kwargs, self.kwargs, init_kwargs, client_kwargs):
            for option in ("region_name", "endpoint_url"):
                if parameters.get(option):
                    self.cache_regions = False
                    break
        else:
            cache_regions = self.cache_regions

        logger.debug(
            "RC: caching enabled? %r (explicit option is %r)",
            cache_regions,
            self.cache_regions,
        )
        self.cache_regions = cache_regions
        if self.cache_regions:
            s3creator = S3BucketRegionCache(
                self.session, config=conf, **init_kwargs, **client_kwargs
            )
            self._s3 = await s3creator.get_client()
        else:
            s3creator = self.session.create_client(
                "s3", config=conf, **init_kwargs, **client_kwargs
            )
            self._s3 = await s3creator.__aenter__()

        self._s3creator = s3creator
        # the following actually closes the aiohttp connection; use of privates
        # might break in the future, would cause exception at gc time
        if not self.asynchronous:
            weakref.finalize(self, self.close_session, self.loop, self._s3creator)
        self._kwargs_helper = ParamKwargsHelper(self._s3)
        return self._s3

    _connect = set_session

    connect = sync_wrapper(set_session)

    @staticmethod
    def close_session(loop, s3):
        if loop is not None and loop.is_running():
            try:
                sync(loop, s3.__aexit__, None, None, None, timeout=0.1)
                return
            except FSTimeoutError:
                pass
        try:
            # close the actual socket
            s3._client._endpoint.http_session._connector._close()
        except AttributeError:
            # but during shutdown, it may have gone
            pass

    async def _get_delegated_s3pars(self, exp=3600):
        """Get temporary credentials from STS, appropriate for sending across a
        network. Only relevant where the key/secret were explicitly provided.

        Parameters
        ----------
        exp : int
            Time in seconds that credentials are good for

        Returns
        -------
        dict of parameters
        """
        if self.anon:
            return {"anon": True}
        if self.token:  # already has temporary cred
            return {
                "key": self.key,
                "secret": self.secret,
                "token": self.token,
                "anon": False,
            }
        if self.key is None or self.secret is None:  # automatic credentials
            return {"anon": False}
        async with self.session.create_client("sts") as sts:
            cred = sts.get_session_token(DurationSeconds=exp)["Credentials"]
            return {
                "key": cred["AccessKeyId"],
                "secret": cred["SecretAccessKey"],
                "token": cred["SessionToken"],
                "anon": False,
            }

    get_delegated_s3pars = sync_wrapper(_get_delegated_s3pars)

    def _open(
        self,
        path,
        mode="rb",
        block_size=None,
        acl="",
        version_id=None,
        fill_cache=None,
        cache_type=None,
        autocommit=True,
        requester_pays=None,
        **kwargs
    ):
        """Open a file for reading or writing

        Parameters
        ----------
        path: string
            Path of file on S3
        mode: string
            One of 'r', 'w', 'a', 'rb', 'wb', or 'ab'. These have the same meaning
            as they do for the built-in `open` function.
        block_size: int
            Size of data-node blocks if reading
        fill_cache: bool
            If seeking to new a part of the file beyond the current buffer,
            with this True, the buffer will be filled between the sections to
            best support random access. When reading only a few specific chunks
            out of a file, performance may be better if False.
        acl: str
            Canned ACL to set when writing
        version_id : str
            Explicit version of the object to open.  This requires that the s3
            filesystem is version aware and bucket versioning is enabled on the
            relevant bucket.
        encoding : str
            The encoding to use if opening the file in text mode. The platform's
            default text encoding is used if not given.
        cache_type : str
            See fsspec's documentation for available cache_type values. Set to "none"
            if no caching is desired. If None, defaults to ``self.default_cache_type``.
        requester_pays : bool (optional)
            If RequesterPays buckets are supported.  If None, defaults to the
            value used when creating the S3FileSystem (which defaults to False.)
        kwargs: dict-like
            Additional parameters used for s3 methods.  Typically used for
            ServerSideEncryption.
        """
        if block_size is None:
            block_size = self.default_block_size
        if fill_cache is None:
            fill_cache = self.default_fill_cache
        if requester_pays is None:
            requester_pays = bool(self.req_kw)

        acl = acl or self.s3_additional_kwargs.get("ACL", "")
        kw = self.s3_additional_kwargs.copy()
        kw.update(kwargs)
        if not self.version_aware and version_id:
            raise ValueError(
                "version_id cannot be specified if the filesystem "
                "is not version aware"
            )

        if cache_type is None:
            cache_type = self.default_cache_type

        return S3File(
            self,
            path,
            mode,
            block_size=block_size,
            acl=acl,
            version_id=version_id,
            fill_cache=fill_cache,
            s3_additional_kwargs=kw,
            cache_type=cache_type,
            autocommit=autocommit,
            requester_pays=requester_pays,
        )

    async def _lsdir(
        self, path, refresh=False, max_items=None, delimiter="/", prefix=""
    ):
        bucket, key, _ = self.split_path(path)
        if not prefix:
            prefix = ""
        if key:
            prefix = key.lstrip("/") + "/" + prefix
        if path not in self.dircache or refresh or not delimiter:
            try:
                logger.debug("Get directory listing page for %s" % path)
                await self.set_session()
                s3 = await self.get_s3(bucket)
                pag = s3.get_paginator("list_objects_v2")
                config = {}
                if max_items is not None:
                    config.update(MaxItems=max_items, PageSize=2 * max_items)
                it = pag.paginate(
                    Bucket=bucket,
                    Prefix=prefix,
                    Delimiter=delimiter,
                    PaginationConfig=config,
                    **self.req_kw,
                )
                files = []
                dircache = []
                async for i in it:
                    dircache.extend(i.get("CommonPrefixes", []))
                    for c in i.get("Contents", []):
                        c["type"] = "file"
                        c["size"] = c["Size"]
                        files.append(c)
                if dircache:
                    files.extend(
                        [
                            {
                                "Key": l["Prefix"][:-1],
                                "Size": 0,
                                "StorageClass": "DIRECTORY",
                                "type": "directory",
                                "size": 0,
                            }
                            for l in dircache
                        ]
                    )
                for f in files:
                    f["Key"] = "/".join([bucket, f["Key"]])
                    f["name"] = f["Key"]
            except ClientError as e:
                raise translate_boto_error(e)

            if delimiter and files:
                self.dircache[path] = files
            return files
        return self.dircache[path]

    async def _find(self, path, maxdepth=None, withdirs=None, detail=False, prefix=""):
        """List all files below path.
        Like posix ``find`` command without conditions
        Parameters
        ----------
        path : str
        maxdepth: int or None
            If not None, the maximum number of levels to descend
        withdirs: bool
            Whether to include directory paths in the output. This is True
            when used by glob, but users usually only want files.
        prefix: str
            Only return files that match ``^{path}/{prefix}`` (if there is an
            exact match ``filename == {path}/{prefix}``, it also will be included)
        """
        path = self._strip_protocol(path)
        bucket, key, _ = self.split_path(path)
        if not bucket:
            raise ValueError("Cannot traverse all of S3")
        if (withdirs or maxdepth) and prefix:
            # TODO: perhaps propagate these to a glob(f"path/{prefix}*") call
            raise ValueError(
                "Can not specify 'prefix' option alongside 'withdirs'/'maxdepth' options."
            )
        if maxdepth:
            return await super()._find(
                bucket + "/" + key, maxdepth=maxdepth, withdirs=withdirs, detail=detail
            )
        # TODO: implement find from dircache, if all listings are present
        # if refresh is False:
        #     out = incomplete_tree_dirs(self.dircache, path)
        #     if len(out) == 1:
        #         await self._find(out[0])
        #         return super().find(path)
        #     elif len(out) == 0:
        #         return super().find(path)
        #     # else: we refresh anyway, having at least two missing trees
        out = await self._lsdir(path, delimiter="", prefix=prefix)
        if not out and key:
            try:
                out = [await self._info(path)]
            except FileNotFoundError:
                out = []
        dirs = []
        sdirs = set()
        thisdircache = {}
        for o in out:
            par = self._parent(o["name"])
            if par not in self.dircache:
                if par not in sdirs:
                    sdirs.add(par)
                    d = False
                    if len(path) <= len(par):
                        d = {
                            "Key": self.split_path(par)[1],
                            "Size": 0,
                            "name": par,
                            "StorageClass": "DIRECTORY",
                            "type": "directory",
                            "size": 0,
                        }
                        dirs.append(d)
                    thisdircache[par] = []
                    ppar = self._parent(par)
                    if ppar in thisdircache:
                        if d and d not in thisdircache[ppar]:
                            thisdircache[ppar].append(d)
            if par in sdirs:
                thisdircache[par].append(o)
        if not prefix:
            for k, v in thisdircache.items():
                if k not in self.dircache and len(k) >= len(path):
                    self.dircache[k] = v
        if withdirs:
            out = sorted(out + dirs, key=lambda x: x["name"])
        if detail:
            return {o["name"]: o for o in out}
        return [o["name"] for o in out]

    find = sync_wrapper(_find)

    async def _mkdir(self, path, acl="", create_parents=True, **kwargs):
        path = self._strip_protocol(path).rstrip("/")
        if not path:
            raise ValueError
        bucket, key, _ = self.split_path(path)
        if await self._exists(bucket):
            if not key:
                # requested to create bucket, but bucket already exist
                raise FileExistsError
            # else: # do nothing as bucket is already created.
        elif not key or create_parents:
            if acl and acl not in buck_acls:
                raise ValueError("ACL not in %s", buck_acls)
            try:
                params = {"Bucket": bucket, "ACL": acl}
                region_name = kwargs.get("region_name", None) or self.client_kwargs.get(
                    "region_name", None
                )
                if region_name:
                    params["CreateBucketConfiguration"] = {
                        "LocationConstraint": region_name
                    }
                await self._call_s3("create_bucket", **params)
                self.invalidate_cache("")
                self.invalidate_cache(bucket)
            except ClientError as e:
                raise translate_boto_error(e)
            except ParamValidationError as e:
                raise ValueError("Bucket create failed %r: %s" % (bucket, e))
        else:
            # raises if bucket doesn't exist and doesn't get create flag.
            await self._ls(bucket)

    mkdir = sync_wrapper(_mkdir)

    async def _makedirs(self, path, exist_ok=False):
        try:
            await self._mkdir(path, create_parents=True)
        except FileExistsError:
            if exist_ok:
                pass
            else:
                raise

    makedirs = sync_wrapper(_makedirs)

    async def _rmdir(self, path):
        try:
            await self._call_s3("delete_bucket", Bucket=path)
        except botocore.exceptions.ClientError as e:
            if "NoSuchBucket" in str(e):
                raise FileNotFoundError(path) from e
            if "BucketNotEmpty" in str(e):
                raise OSError from e
            raise
        self.invalidate_cache(path)
        self.invalidate_cache("")

    rmdir = sync_wrapper(_rmdir)

    async def _lsbuckets(self, refresh=False):
        if "" not in self.dircache or refresh:
            if self.anon:
                # cannot list buckets if not logged in
                return []
            try:
                files = (await self._call_s3("list_buckets"))["Buckets"]
            except ClientError:
                # listbucket permission missing
                return []
            for f in files:
                f["Key"] = f["Name"]
                f["Size"] = 0
                f["StorageClass"] = "BUCKET"
                f["size"] = 0
                f["type"] = "directory"
                f["name"] = f["Name"]
                del f["Name"]
            self.dircache[""] = files
            return files
        return self.dircache[""]

    async def _ls(self, path, detail=False, refresh=False):
        """List files in given bucket, or list of buckets.

        Listing is cached unless `refresh=True`.

        Note: only your buckets associated with the login will be listed by
        `ls('')`, not any public buckets (even if already accessed).

        Parameters
        ----------
        path : string/bytes
            location at which to list files
        refresh : bool (=False)
            if False, look in local cache for file details first
        """
        path = self._strip_protocol(path).rstrip("/")
        if path in ["", "/"]:
            files = await self._lsbuckets(refresh)
        else:
            files = await self._lsdir(path, refresh)
            if not files and "/" in path:
                files = await self._lsdir(self._parent(path), refresh=refresh)
                files = [
                    o
                    for o in files
                    if o["name"].rstrip("/") == path and o["type"] != "directory"
                ]
            if detail:
                return files
        return files if detail else sorted([o["name"] for o in files])

    async def _exists(self, path):
        if path in ["", "/"]:
            # the root always exists, even if anon
            return True
        path = self._strip_protocol(path)
        bucket, key, version_id = self.split_path(path)
        if key:
            try:
                if self._ls_from_cache(path):
                    return True
            except FileNotFoundError:
                return False
            try:
                await self._info(path, bucket, key, version_id=version_id)
                return True
            except FileNotFoundError:
                return False
        elif self.dircache.get(bucket, False):
            return True
        else:
            try:
                if self._ls_from_cache(bucket):
                    return True
            except FileNotFoundError:
                # might still be a bucket we can access but don't own
                pass
            try:
                await self._call_s3(
                    "list_objects_v2", MaxKeys=1, Bucket=bucket, **self.req_kw
                )
                return True
            except Exception:
                return False

    exists = sync_wrapper(_exists)

    async def _touch(self, path, truncate=True, data=None, **kwargs):
        """Create empty file or truncate"""
        bucket, key, version_id = self.split_path(path)
        if version_id:
            raise ValueError("S3 does not support touching existing versions of files")
        if not truncate and await self._exists(path):
            raise ValueError("S3 does not support touching existent files")
        try:
            write_result = await self._call_s3(
                "put_object", Bucket=bucket, Key=key, **kwargs
            )
        except ClientError as ex:
            raise translate_boto_error(ex)
        self.invalidate_cache(self._parent(path))
        return write_result

    touch = sync_wrapper(_touch)

    async def _cat_file(self, path, version_id=None, start=None, end=None):
        bucket, key, vers = self.split_path(path)
        if start is not None or end is not None:
            head = {"Range": await self._process_limits(path, start, end)}
        else:
            head = {}
        resp = await self._call_s3(
            "get_object",
            Bucket=bucket,
            Key=key,
            **version_id_kw(version_id or vers),
            **head,
            **self.req_kw,
        )
        data = await resp["Body"].read()
        resp["Body"].close()
        return data

    async def _pipe_file(self, path, data, chunksize=50 * 2 ** 20, **kwargs):
        bucket, key, _ = self.split_path(path)
        size = len(data)
        # 5 GB is the limit for an S3 PUT
        if size < min(5 * 2 ** 30, 2 * chunksize):
            return await self._call_s3(
                "put_object", Bucket=bucket, Key=key, Body=data, **kwargs
            )
        else:

            mpu = await self._call_s3(
                "create_multipart_upload", Bucket=bucket, Key=key, **kwargs
            )

            out = [
                await self._call_s3(
                    "upload_part",
                    Bucket=bucket,
                    PartNumber=i + 1,
                    UploadId=mpu["UploadId"],
                    Body=data[off : off + chunksize],
                    Key=key,
                )
                for i, off in enumerate(range(0, len(data), chunksize))
            ]

            parts = [
                {"PartNumber": i + 1, "ETag": o["ETag"]} for i, o in enumerate(out)
            ]
            await self._call_s3(
                "complete_multipart_upload",
                Bucket=bucket,
                Key=key,
                UploadId=mpu["UploadId"],
                MultipartUpload={"Parts": parts},
            )
        self.invalidate_cache(path)

    async def _put_file(self, lpath, rpath, chunksize=50 * 2 ** 20, **kwargs):
        bucket, key, _ = self.split_path(rpath)
        if os.path.isdir(lpath):
            if key:
                # don't make remote "directory"
                return
            else:
                await self._mkdir(lpath)
        size = os.path.getsize(lpath)
        with open(lpath, "rb") as f0:
            if size < min(5 * 2 ** 30, 2 * chunksize):
                await self._call_s3(
                    "put_object", Bucket=bucket, Key=key, Body=f0, **kwargs
                )
            else:

                mpu = await self._call_s3(
                    "create_multipart_upload", Bucket=bucket, Key=key, **kwargs
                )

                out = []
                while True:
                    chunk = f0.read(chunksize)
                    if not chunk:
                        break
                    out.append(
                        await self._call_s3(
                            "upload_part",
                            Bucket=bucket,
                            PartNumber=len(out) + 1,
                            UploadId=mpu["UploadId"],
                            Body=chunk,
                            Key=key,
                        )
                    )

                parts = [
                    {"PartNumber": i + 1, "ETag": o["ETag"]} for i, o in enumerate(out)
                ]
                await self._call_s3(
                    "complete_multipart_upload",
                    Bucket=bucket,
                    Key=key,
                    UploadId=mpu["UploadId"],
                    MultipartUpload={"Parts": parts},
                )
        while rpath:
            self.invalidate_cache(rpath)
            rpath = self._parent(rpath)

    async def _get_file(self, rpath, lpath, version_id=None):
        bucket, key, vers = self.split_path(rpath)
        if os.path.isdir(lpath):
            return
        resp = await self._call_s3(
            "get_object",
            Bucket=bucket,
            Key=key,
            **version_id_kw(version_id or vers),
            **self.req_kw,
        )
        body = resp["Body"]
        try:
            with open(lpath, "wb") as f0:
                while True:
                    chunk = await body.read(2 ** 16)
                    if not chunk:
                        break
                    f0.write(chunk)
        finally:
            body.close()

    async def _simple_info(self, path):
        bucket, key, _ = self.split_path(path)
        prefix = key.strip("/")

        out = await self._call_s3(
            "list_objects_v2",
            self.kwargs,
            Bucket=bucket,
            Prefix=prefix,
            Delimiter="/",
            MaxKeys=1,
            **self.req_kw,
        )
        # This method either can return the info blob for the object if it
        # exists, or return None if it doesn't exist as a file and it's
        # existence as a directory is undeterminable with this call. If we
        # can determine that no file or directory exists on the S3 with this
        # prefix, then we can safely raise the FileNotFoundError.
        [file] = out.get("Contents", [None])
        if file and file["Key"] == prefix:
            return {
                "ETag": file["ETag"],
                "Key": "/".join([bucket, key]),
                "LastModified": file["LastModified"],
                "Size": file["Size"],
                "size": file["Size"],
                "name": "/".join([bucket, key]),
                "type": "file",
                "StorageClass": "STANDARD",
                "VersionId": file.get("VersionId"),
            }
        elif not (file or out.get("KeyCount", 0) or out.get("CommonPrefixes", [])):
            raise FileNotFoundError(path)

    async def _version_aware_info(self, path, version_id):
        bucket, key, _ = self.split_path(path)

        try:
            out = await self._call_s3(
                "head_object",
                self.kwargs,
                Bucket=bucket,
                Key=key,
                **version_id_kw(version_id),
                **self.req_kw,
            )
        except FileNotFoundError:
            return None

        return {
            "ETag": out["ETag"],
            "Key": "/".join([bucket, key]),
            "LastModified": out["LastModified"],
            "Size": out["ContentLength"],
            "size": out["ContentLength"],
            "name": "/".join([bucket, key]),
            "type": "file",
            "StorageClass": "STANDARD",
            "VersionId": out.get("VersionId"),
        }

    async def _info(self, path, bucket=None, key=None, refresh=False, version_id=None):
        path = self._strip_protocol(path)
        bucket, key, path_version_id = self.split_path(path)
        if version_id is not None:
            if not self.version_aware:
                raise ValueError(
                    "version_id cannot be specified if the "
                    "filesystem is not version aware"
                )
        if path in ["/", ""]:
            return {"name": path, "size": 0, "type": "directory"}
        version_id = _coalesce_version_id(path_version_id, version_id)
        if not refresh and self._ls_from_cache(path) is not None:
            out = self._ls_from_cache(path)
            out = [o for o in out if o["name"] == path]
            if out:
                return out[0]
            return {"name": path, "size": 0, "type": "directory"}
        if key:
            try:
                # The HeadObject calls are really expensive on objects
                # compared to a ListObjectsV2 call with a max_keys=1. The
                # disadvantage of the list objects is that, it doesn't yield
                # a list of versions which might be needed on version_aware filesystems.
                # So we switch between a normal HeadObject call (when version_aware is set)
                # and ListObjectsV2 (when it is not).
                if self.version_aware:
                    out = await self._version_aware_info(path, version_id)
                else:
                    try:
                        out = await self._simple_info(path)
                    except PermissionError:
                        # If the permissions aren't enough for scanning a prefix
                        # then fall back to using normal HEAD_OBJECT
                        out = await self._version_aware_info(path, version_id)
                if out:
                    return out
            except ClientError as e:
                raise translate_boto_error(e, set_cause=False)

        try:
            # We check to see if the path is a directory by attempting to list its
            # contexts. If anything is found, it is indeed a directory
            out = await self._call_s3(
                "list_objects_v2",
                self.kwargs,
                Bucket=bucket,
                Prefix=key.rstrip("/") + "/" if key else "",
                Delimiter="/",
                MaxKeys=1,
                **self.req_kw,
            )
            if (
                out.get("KeyCount", 0) > 0
                or out.get("Contents", [])
                or out.get("CommonPrefixes", [])
            ):
                return {
                    "Key": "/".join([bucket, key]),
                    "name": "/".join([bucket, key]),
                    "type": "directory",
                    "Size": 0,
                    "size": 0,
                    "StorageClass": "DIRECTORY",
                }

            raise FileNotFoundError(path)
        except ClientError as e:
            raise translate_boto_error(e, set_cause=False)
        except ParamValidationError as e:
            raise ValueError("Failed to list path %r: %s" % (path, e))

    async def _checksum(self, path, refresh=False):
        """
        Unique value for current version of file

        If the checksum is the same from one moment to another, the contents
        are guaranteed to be the same. If the checksum changes, the contents
        *might* have changed.

        Parameters
        ----------
        path : string/bytes
            path of file to get checksum for
        refresh : bool (=False)
            if False, look in local cache for file details first

        """

        info = await self._info(path, refresh=refresh)

        if info["type"] != "directory":
            return int(info["ETag"].strip('"').split("-")[0], 16)
        else:
            return int(tokenize(info), 16)

    checksum = sync_wrapper(_checksum)

    async def _isdir(self, path):
        path = self._strip_protocol(path).strip("/")
        # Send buckets to super
        if "/" not in path:
            if path in await self._lsbuckets(path):
                return True
            try:
                out = await self._lsdir(path)
                return True
            except FileNotFoundError:
                return False

        if path in self.dircache:
            for fp in self.dircache[path]:
                # For files the dircache can contain itself.
                # If it contains anything other than itself it is a directory.
                if fp["name"] != path:
                    return True
            return False

        parent = self._parent(path)
        if parent in self.dircache:
            for f in self.dircache[parent]:
                if f["name"] == path:
                    # If we find ourselves return whether we are a directory
                    return f["type"] == "directory"
            return False

        # This only returns things within the path and NOT the path object itself
        return bool(await self._lsdir(path))

    isdir = sync_wrapper(_isdir)

    async def _object_version_info(self, path, **kwargs):
        if not self.version_aware:
            raise ValueError(
                "version specific functionality is disabled for "
                "non-version aware filesystems"
            )
        bucket, key, _ = self.split_path(path)
        kwargs = {}
        out = {"IsTruncated": True}
        versions = []
        while out["IsTruncated"]:
            out = await self._call_s3(
                "list_object_versions",
                kwargs,
                Bucket=bucket,
                Prefix=key,
                **self.req_kw,
            )
            versions.extend(out["Versions"])
            kwargs.update(
                {
                    "VersionIdMarker": out.get("NextVersionIdMarker", ""),
                    "KeyMarker": out.get("NextKeyMarker", ""),
                }
            )
        return versions

    object_version_info = sync_wrapper(_object_version_info)

    _metadata_cache = {}

    async def _metadata(self, path, refresh=False, **kwargs):
        """Return metadata of path.

        Metadata is cached unless `refresh=True`.

        Parameters
        ----------
        path : string/bytes
            filename to get metadata for
        refresh : bool (=False)
            if False, look in local cache for file metadata first
        """
        bucket, key, version_id = self.split_path(path)
        if refresh or path not in self._metadata_cache:
            response = await self._call_s3(
                "head_object",
                kwargs,
                Bucket=bucket,
                Key=key,
                **version_id_kw(version_id),
                **self.req_kw,
            )
            meta = {k.replace("_", "-"): v for k, v in response["Metadata"].items()}
            self._metadata_cache[path] = meta

        return self._metadata_cache[path]

    metadata = sync_wrapper(_metadata)

    def get_tags(self, path):
        """Retrieve tag key/values for the given path

        Returns
        -------
        {str: str}
        """
        bucket, key, version_id = self.split_path(path)
        response = self.call_s3(
            "get_object_tagging",
            Bucket=bucket,
            Key=key,
            **version_id_kw(version_id),
        )
        return {v["Key"]: v["Value"] for v in response["TagSet"]}

    def put_tags(self, path, tags, mode="o"):
        """Set tags for given existing key

        Tags are a str:str mapping that can be attached to any key, see
        https://docs.aws.amazon.com/awsaccountbilling/latest/aboutv2/allocation-tag-restrictions.html

        This is similar to, but distinct from, key metadata, which is usually
        set at key creation time.

        Parameters
        ----------
        path: str
            Existing key to attach tags to
        tags: dict str, str
            Tags to apply.
        mode:
            One of 'o' or 'm'
            'o': Will over-write any existing tags.
            'm': Will merge in new tags with existing tags.  Incurs two remote
            calls.
        """
        bucket, key, version_id = self.split_path(path)

        if mode == "m":
            existing_tags = self.get_tags(path=path)
            existing_tags.update(tags)
            new_tags = [{"Key": k, "Value": v} for k, v in existing_tags.items()]
        elif mode == "o":
            new_tags = [{"Key": k, "Value": v} for k, v in tags.items()]
        else:
            raise ValueError("Mode must be {'o', 'm'}, not %s" % mode)

        tag = {"TagSet": new_tags}
        self.call_s3(
            "put_object_tagging",
            Bucket=bucket,
            Key=key,
            Tagging=tag,
            **version_id_kw(version_id),
        )

    async def _getxattr(self, path, attr_name, **kwargs):
        """Get an attribute from the metadata.

        Examples
        --------
        >>> mys3fs.getxattr('mykey', 'attribute_1')  # doctest: +SKIP
        'value_1'
        """
        attr_name = attr_name.replace("_", "-")
        xattr = await self._metadata(path, **kwargs)
        if attr_name in xattr:
            return xattr[attr_name]
        return None

    getxattr = sync_wrapper(_getxattr)

    async def _setxattr(self, path, copy_kwargs=None, **kw_args):
        """Set metadata.

        Attributes have to be of the form documented in the
        `Metadata Reference`_.

        Parameters
        ----------
        kw_args : key-value pairs like field="value", where the values must be
            strings. Does not alter existing fields, unless
            the field appears here - if the value is None, delete the
            field.
        copy_kwargs : dict, optional
            dictionary of additional params to use for the underlying
            s3.copy_object.

        Examples
        --------
        >>> mys3file.setxattr(attribute_1='value1', attribute_2='value2')  # doctest: +SKIP
        # Example for use with copy_args
        >>> mys3file.setxattr(copy_kwargs={'ContentType': 'application/pdf'},
        ...     attribute_1='value1')  # doctest: +SKIP


        .. Metadata Reference:
        http://docs.aws.amazon.com/AmazonS3/latest/dev/UsingMetadata.html#object-metadata
        """

        kw_args = {k.replace("_", "-"): v for k, v in kw_args.items()}
        bucket, key, version_id = self.split_path(path)
        metadata = await self._metadata(path)
        metadata.update(**kw_args)
        copy_kwargs = copy_kwargs or {}

        # remove all keys that are None
        for kw_key in kw_args:
            if kw_args[kw_key] is None:
                metadata.pop(kw_key, None)

        src = {"Bucket": bucket, "Key": key}
        if version_id:
            src["VersionId"] = version_id

        await self._call_s3(
            "copy_object",
            copy_kwargs,
            CopySource=src,
            Bucket=bucket,
            Key=key,
            Metadata=metadata,
            MetadataDirective="REPLACE",
        )

        # refresh metadata
        self._metadata_cache[path] = metadata

    setxattr = sync_wrapper(_setxattr)

    async def _chmod(self, path, acl, **kwargs):
        """Set Access Control on a bucket/key

        See http://docs.aws.amazon.com/AmazonS3/latest/dev/acl-overview.html#canned-acl

        Parameters
        ----------
        path : string
            the object to set
        acl : string
            the value of ACL to apply
        """
        bucket, key, version_id = self.split_path(path)
        if key:
            if acl not in key_acls:
                raise ValueError("ACL not in %s", key_acls)
            await self._call_s3(
                "put_object_acl",
                kwargs,
                Bucket=bucket,
                Key=key,
                ACL=acl,
                **version_id_kw(version_id),
            )
        else:
            if acl not in buck_acls:
                raise ValueError("ACL not in %s", buck_acls)
            await self._call_s3("put_bucket_acl", kwargs, Bucket=bucket, ACL=acl)

    chmod = sync_wrapper(_chmod)

    async def _url(self, path, expires=3600, **kwargs):
        """Generate presigned URL to access path by HTTP

        Parameters
        ----------
        path : string
            the key path we are interested in
        expires : int
            the number of seconds this signature will be good for.
        """
        bucket, key, version_id = self.split_path(path)
        await self.set_session()
        s3 = await self.get_s3(bucket)
        return await s3.generate_presigned_url(
            ClientMethod="get_object",
            Params=dict(Bucket=bucket, Key=key, **version_id_kw(version_id), **kwargs),
            ExpiresIn=expires,
        )

    url = sync_wrapper(_url)

    async def _merge(self, path, filelist, **kwargs):
        """Create single S3 file from list of S3 files

        Uses multi-part, no data is downloaded. The original files are
        not deleted.

        Parameters
        ----------
        path : str
            The final file to produce
        filelist : list of str
            The paths, in order, to assemble into the final file.
        """
        bucket, key, version_id = self.split_path(path)
        if version_id:
            raise ValueError("Cannot write to an explicit versioned file!")
        mpu = await self._call_s3(
            "create_multipart_upload", kwargs, Bucket=bucket, Key=key
        )
        # TODO: Make this support versions?
        out = await asyncio.gather(
            *[
                self._call_s3(
                    "upload_part_copy",
                    kwargs,
                    Bucket=bucket,
                    Key=key,
                    UploadId=mpu["UploadId"],
                    CopySource=f,
                    PartNumber=i + 1,
                )
                for (i, f) in enumerate(filelist)
            ]
        )
        parts = [
            {"PartNumber": i + 1, "ETag": o["CopyPartResult"]["ETag"]}
            for (i, o) in enumerate(out)
        ]
        part_info = {"Parts": parts}
        await self._call_s3(
            "complete_multipart_upload",
            Bucket=bucket,
            Key=key,
            UploadId=mpu["UploadId"],
            MultipartUpload=part_info,
        )
        self.invalidate_cache(path)

    merge = sync_wrapper(_merge)

    async def _copy_basic(self, path1, path2, **kwargs):
        """Copy file between locations on S3

        Not allowed where the origin is >5GB - use copy_managed
        """
        buc1, key1, ver1 = self.split_path(path1)
        buc2, key2, ver2 = self.split_path(path2)
        if ver2:
            raise ValueError("Cannot copy to a versioned file!")
        try:
            copy_src = {"Bucket": buc1, "Key": key1}
            if ver1:
                copy_src["VersionId"] = ver1
            await self._call_s3(
                "copy_object", kwargs, Bucket=buc2, Key=key2, CopySource=copy_src
            )
        except ClientError as e:
            raise translate_boto_error(e)
        except ParamValidationError as e:
            raise ValueError("Copy failed (%r -> %r): %s" % (path1, path2, e)) from e
        self.invalidate_cache(path2)

    async def _copy_etag_preserved(self, path1, path2, size, total_parts, **kwargs):
        """Copy file between locations on S3 as multi-part while preserving
        the etag (using the same part sizes for each part"""

        bucket1, key1, version1 = self.split_path(path1)
        bucket2, key2, version2 = self.split_path(path2)

        mpu = await self._call_s3(
            "create_multipart_upload", Bucket=bucket2, Key=key2, **kwargs
        )
        part_infos = await asyncio.gather(
            *[
                self._call_s3("head_object", Bucket=bucket1, Key=key1, PartNumber=i)
                for i in range(1, total_parts + 1)
            ]
        )

        parts = []
        brange_first = 0
        for i, part_info in enumerate(part_infos, 1):
            part_size = part_info["ContentLength"]
            brange_last = brange_first + part_size - 1
            if brange_last > size:
                brange_last = size - 1

            part = await self._call_s3(
                "upload_part_copy",
                Bucket=bucket2,
                Key=key2,
                PartNumber=i,
                UploadId=mpu["UploadId"],
                CopySource=path1,
                CopySourceRange="bytes=%i-%i" % (brange_first, brange_last),
            )
            parts.append({"PartNumber": i, "ETag": part["CopyPartResult"]["ETag"]})
            brange_first += part_size

        await self._call_s3(
            "complete_multipart_upload",
            Bucket=bucket2,
            Key=key2,
            UploadId=mpu["UploadId"],
            MultipartUpload={"Parts": parts},
        )
        self.invalidate_cache(path2)

    async def _copy_managed(self, path1, path2, size, block=5 * 2 ** 30, **kwargs):
        """Copy file between locations on S3 as multi-part

        block: int
            The size of the pieces, must be larger than 5MB and at most 5GB.
            Smaller blocks mean more calls, only useful for testing.
        """
        if block < 5 * 2 ** 20 or block > 5 * 2 ** 30:
            raise ValueError("Copy block size must be 5MB<=block<=5GB")
        bucket, key, version = self.split_path(path2)
        mpu = await self._call_s3(
            "create_multipart_upload", Bucket=bucket, Key=key, **kwargs
        )
        # attempting to do the following calls concurrently with gather causes
        # occasional "upload is smaller than the minimum allowed"
        out = [
            await self._call_s3(
                "upload_part_copy",
                Bucket=bucket,
                Key=key,
                PartNumber=i + 1,
                UploadId=mpu["UploadId"],
                CopySource=path1,
                CopySourceRange="bytes=%i-%i" % (brange_first, brange_last),
            )
            for i, (brange_first, brange_last) in enumerate(_get_brange(size, block))
        ]
        parts = [
            {"PartNumber": i + 1, "ETag": o["CopyPartResult"]["ETag"]}
            for i, o in enumerate(out)
        ]
        await self._call_s3(
            "complete_multipart_upload",
            Bucket=bucket,
            Key=key,
            UploadId=mpu["UploadId"],
            MultipartUpload={"Parts": parts},
        )
        self.invalidate_cache(path2)

    async def _cp_file(self, path1, path2, preserve_etag=None, **kwargs):
        """Copy file between locations on S3.

        preserve_etag: bool
            Whether to preserve etag while copying. If the file is uploaded
            as a single part, then it will be always equalivent to the md5
            hash of the file hence etag will always be preserved. But if the
            file is uploaded in multi parts, then this option will try to
            reproduce the same multipart upload while copying and preserve
            the generated etag.
        """
        path1 = self._strip_protocol(path1)
        bucket, key, vers = self.split_path(path1)

        info = await self._info(path1, bucket, key, version_id=vers)
        size = info["size"]

        _, _, parts_suffix = info.get("ETag", "").strip('"').partition("-")
        if preserve_etag and parts_suffix:
            await self._copy_etag_preserved(
                path1, path2, size, total_parts=int(parts_suffix)
            )
        elif size <= MANAGED_COPY_THRESHOLD:
            # simple copy allowed for <5GB
            await self._copy_basic(path1, path2, **kwargs)
        else:
            # if the preserve_etag is true, either the file is uploaded
            # on multiple parts or the size is lower than 5GB
            assert not preserve_etag

            # serial multipart copy
            await self._copy_managed(path1, path2, size, **kwargs)

    async def _clear_multipart_uploads(self, bucket):
        """Remove any partial uploads in the bucket"""
        out = await self._call_s3("list_multipart_uploads", Bucket=bucket)
        await asyncio.gather(
            *[
                self._call_s3(
                    "abort_multipart_upload",
                    Bucket=bucket,
                    Key=upload["Key"],
                    UploadId=upload["UploadId"],
                )
                for upload in out["Contents"]
            ]
        )

    async def _bulk_delete(self, pathlist, **kwargs):
        """
        Remove multiple keys with one call

        Parameters
        ----------
        pathlist : list(str)
            The keys to remove, must all be in the same bucket.
            Must have 0 < len <= 1000
        """
        if not pathlist:
            return
        buckets = {self.split_path(path)[0] for path in pathlist}
        if len(buckets) > 1:
            raise ValueError("Bulk delete files should refer to only one bucket")
        bucket = buckets.pop()
        if len(pathlist) > 1000:
            raise ValueError("Max number of files to delete in one call is 1000")
        delete_keys = {
            "Objects": [{"Key": self.split_path(path)[1]} for path in pathlist],
            "Quiet": True,
        }
        for path in pathlist:
            self.invalidate_cache(self._parent(path))
        await self._call_s3("delete_objects", kwargs, Bucket=bucket, Delete=delete_keys)

    async def _rm_file(self, path, **kwargs):
        bucket, key, _ = self.split_path(path)

        try:
            await self._call_s3("delete_object", Bucket=bucket, Key=key)
        except ClientError as e:
            raise translate_boto_error(e)

    async def _rm(self, path, recursive=False, **kwargs):
        if recursive and isinstance(path, str):
            bucket, key, _ = self.split_path(path)
            if not key and await self._is_bucket_versioned(bucket):
                # special path to completely remove versioned bucket
                await self._rm_versioned_bucket_contents(self, bucket)
        paths = await self._expand_path(path, recursive=recursive)
        files = [p for p in paths if self.split_path(p)[1]]
        dirs = [p for p in paths if not self.split_path(p)[1]]
        # TODO: fails if more than one bucket in list
        await asyncio.gather(
            *[
                self._bulk_delete(files[i : i + 1000])
                for i in range(0, len(files), 1000)
            ]
        )
        await asyncio.gather(*[self._rmdir(d) for d in dirs])
        [
            (self.invalidate_cache(p), self.invalidate_cache(self._parent(p)))
            for p in paths
        ]

    async def _is_bucket_versioned(self, bucket):
        return (await self._call_s3("get_bucket_versioning", Bucket=bucket)).get(
            "Status", ""
        ) == "Enabled"

    is_bucket_versioned = sync_wrapper(_is_bucket_versioned)

    async def _rm_versioned_bucket_contents(self, bucket):
        """Remove a versioned bucket and all contents"""
        await self.set_session()
        s3 = await self.get_s3(bucket)
        pag = s3.get_paginator("list_object_versions")
        async for plist in pag.paginate(Bucket=bucket):
            obs = plist.get("Versions", []) + plist.get("DeleteMarkers", [])
            delete_keys = {
                "Objects": [
                    {"Key": i["Key"], "VersionId": i["VersionId"]} for i in obs
                ],
                "Quiet": True,
            }
            if obs:
                await self._call_s3("delete_objects", Bucket=bucket, Delete=delete_keys)

    def invalidate_cache(self, path=None):
        if path is None:
            self.dircache.clear()
        else:
            path = self._strip_protocol(path)
            self.dircache.pop(path, None)
            while path:
                self.dircache.pop(path, None)
                path = self._parent(path)

    async def _walk(self, path, maxdepth=None, **kwargs):
        if path in ["", "*"] + ["{}://".format(p) for p in self.protocol]:
            raise ValueError("Cannot crawl all of S3")
        async for _ in super()._walk(path, maxdepth=maxdepth, **kwargs):
            yield _

    def modified(self, path, version_id=None, refresh=False):
        """Return the last modified timestamp of file at `path` as a datetime"""
        info = self.info(path=path, version_id=version_id, refresh=refresh)
        if "LastModified" not in info:
            # This path is a bucket or folder, which do not currently have a modified date
            raise IsADirectoryError
        return info["LastModified"].replace(tzinfo=None)

    def sign(self, path, expiration=100, **kwargs):
        return self.url(path, expires=expiration, **kwargs)

    async def _invalidate_region_cache(self):
        """Invalidate the region cache (associated with buckets)
        if ``cache_regions`` is turned on."""
        if not self.cache_regions:
            return None

        # If the region cache is not initialized, then
        # do nothing.
        cache = getattr(self, "_s3creator", None)
        if cache is not None:
            await cache.clear()

    invalidate_region_cache = sync_wrapper(_invalidate_region_cache)


class S3File(AbstractBufferedFile):
    """
    Open S3 key as a file. Data is only loaded and cached on demand.

    Parameters
    ----------
    s3 : S3FileSystem
        botocore connection
    path : string
        S3 bucket/key to access
    mode : str
        One of 'rb', 'wb', 'ab'. These have the same meaning
        as they do for the built-in `open` function.
    block_size : int
        read-ahead size for finding delimiters
    fill_cache : bool
        If seeking to new a part of the file beyond the current buffer,
        with this True, the buffer will be filled between the sections to
        best support random access. When reading only a few specific chunks
        out of a file, performance may be better if False.
    acl: str
        Canned ACL to apply
    version_id : str
        Optional version to read the file at.  If not specified this will
        default to the current version of the object.  This is only used for
        reading.
    requester_pays : bool (False)
        If RequesterPays buckets are supported.

    Examples
    --------
    >>> s3 = S3FileSystem()  # doctest: +SKIP
    >>> with s3.open('my-bucket/my-file.txt', mode='rb') as f:  # doctest: +SKIP
    ...     ...  # doctest: +SKIP

    See Also
    --------
    S3FileSystem.open: used to create ``S3File`` objects

    """

    retries = 5
    part_min = 5 * 2 ** 20
    part_max = 5 * 2 ** 30

    def __init__(
        self,
        s3,
        path,
        mode="rb",
        block_size=5 * 2 ** 20,
        acl="",
        version_id=None,
        fill_cache=True,
        s3_additional_kwargs=None,
        autocommit=True,
        cache_type="bytes",
        requester_pays=False,
    ):
        bucket, key, path_version_id = s3.split_path(path)
        if not key:
            raise ValueError("Attempt to open non key-like path: %s" % path)
        self.bucket = bucket
        self.key = key
        self.version_id = _coalesce_version_id(version_id, path_version_id)
        self.acl = acl
        if self.acl and self.acl not in key_acls:
            raise ValueError("ACL not in %s", key_acls)
        self.mpu = None
        self.parts = None
        self.fill_cache = fill_cache
        self.s3_additional_kwargs = s3_additional_kwargs or {}
        self.req_kw = {"RequestPayer": "requester"} if requester_pays else {}
        if "r" not in mode:
            if block_size < 5 * 2 ** 20:
                raise ValueError("Block size must be >=5MB")
        else:
            if version_id and s3.version_aware:
                self.version_id = version_id
                self.details = s3.info(path, version_id=version_id)
                self.size = self.details["size"]
            elif s3.version_aware:
                # In this case we have not managed to get the VersionId out of details and
                # we should invalidate the cache and perform a full head_object since it
                # has likely been partially populated by ls.
                s3.invalidate_cache(path)
                self.details = s3.info(path)
                self.version_id = self.details.get("VersionId")
        super().__init__(
            s3, path, mode, block_size, autocommit=autocommit, cache_type=cache_type
        )
        self.s3 = self.fs  # compatibility

        # when not using autocommit we want to have transactional state to manage
        self.append_block = False

        if "a" in mode and s3.exists(path):
            loc = s3.info(path)["size"]
            if loc < 5 * 2 ** 20:
                # existing file too small for multi-upload: download
                self.write(self.fs.cat(self.path))
            else:
                self.append_block = True
            self.loc = loc

        if "r" in mode:
            self.req_kw["IfMatch"] = self.details["ETag"]

    def _call_s3(self, method, *kwarglist, **kwargs):
        return self.fs.call_s3(method, self.s3_additional_kwargs, *kwarglist, **kwargs)

    def _initiate_upload(self):
        if self.autocommit and not self.append_block and self.tell() < self.blocksize:
            # only happens when closing small file, use on-shot PUT
            return
        logger.debug("Initiate upload for %s" % self)
        self.parts = []
        self.mpu = self._call_s3(
            "create_multipart_upload",
            Bucket=self.bucket,
            Key=self.key,
            ACL=self.acl,
        )

        if self.append_block:
            # use existing data in key when appending,
            # and block is big enough
            out = self._call_s3(
                "upload_part_copy",
                self.s3_additional_kwargs,
                Bucket=self.bucket,
                Key=self.key,
                PartNumber=1,
                UploadId=self.mpu["UploadId"],
                CopySource=self.path,
            )
            self.parts.append({"PartNumber": 1, "ETag": out["CopyPartResult"]["ETag"]})

    def metadata(self, refresh=False, **kwargs):
        """Return metadata of file.
        See :func:`~s3fs.S3Filesystem.metadata`.

        Metadata is cached unless `refresh=True`.
        """
        return self.fs.metadata(self.path, refresh, **kwargs)

    def getxattr(self, xattr_name, **kwargs):
        """Get an attribute from the metadata.
        See :func:`~s3fs.S3Filesystem.getxattr`.

        Examples
        --------
        >>> mys3file.getxattr('attribute_1')  # doctest: +SKIP
        'value_1'
        """
        return self.fs.getxattr(self.path, xattr_name, **kwargs)

    def setxattr(self, copy_kwargs=None, **kwargs):
        """Set metadata.
        See :func:`~s3fs.S3Filesystem.setxattr`.

        Examples
        --------
        >>> mys3file.setxattr(attribute_1='value1', attribute_2='value2')  # doctest: +SKIP
        """
        if self.writable():
            raise NotImplementedError(
                "cannot update metadata while file " "is open for writing"
            )
        return self.fs.setxattr(self.path, copy_kwargs=copy_kwargs, **kwargs)

    def url(self, **kwargs):
        """HTTP URL to read this file (if it already exists)"""
        return self.fs.url(self.path, **kwargs)

    def _fetch_range(self, start, end):
        try:
            return _fetch_range(
                self.fs,
                self.bucket,
                self.key,
                self.version_id,
                start,
                end,
                req_kw=self.req_kw,
            )

        except OSError as ex:
            if ex.args[0] == errno.EINVAL and "pre-conditions" in ex.args[1]:
                raise FileExpired(
                    filename=self.details["name"], e_tag=self.details["ETag"]
                ) from ex
            else:
                raise

    def _upload_chunk(self, final=False):
        bucket, key, _ = self.fs.split_path(self.path)
        logger.debug(
            "Upload for %s, final=%s, loc=%s, buffer loc=%s"
            % (self, final, self.loc, self.buffer.tell())
        )
        if (
            self.autocommit
            and not self.append_block
            and final
            and self.tell() < self.blocksize
        ):
            # only happens when closing small file, use on-shot PUT
            data1 = False
        else:
            self.buffer.seek(0)
            (data0, data1) = (None, self.buffer.read(self.blocksize))

        while data1:
            (data0, data1) = (data1, self.buffer.read(self.blocksize))
            data1_size = len(data1)

            if 0 < data1_size < self.blocksize:
                remainder = data0 + data1
                remainder_size = self.blocksize + data1_size

                if remainder_size <= self.part_max:
                    (data0, data1) = (remainder, None)
                else:
                    partition = remainder_size // 2
                    (data0, data1) = (remainder[:partition], remainder[partition:])

            part = len(self.parts) + 1
            logger.debug("Upload chunk %s, %s" % (self, part))

            out = self._call_s3(
                "upload_part",
                Bucket=bucket,
                PartNumber=part,
                UploadId=self.mpu["UploadId"],
                Body=data0,
                Key=key,
            )

            self.parts.append({"PartNumber": part, "ETag": out["ETag"]})

        if self.autocommit and final:
            self.commit()
        return not final

    def commit(self):
        logger.debug("Commit %s" % self)
        if self.tell() == 0:
            if self.buffer is not None:
                logger.debug("Empty file committed %s" % self)
                self._abort_mpu()
                write_result = self.fs.touch(self.path)
        elif not self.parts:
            if self.buffer is not None:
                logger.debug("One-shot upload of %s" % self)
                self.buffer.seek(0)
                data = self.buffer.read()
                write_result = self._call_s3(
                    "put_object",
                    Key=self.key,
                    Bucket=self.bucket,
                    Body=data,
                    **self.kwargs,
                )
            else:
                raise RuntimeError
        else:
            logger.debug("Complete multi-part upload for %s " % self)
            part_info = {"Parts": self.parts}
            write_result = self._call_s3(
                "complete_multipart_upload",
                Bucket=self.bucket,
                Key=self.key,
                UploadId=self.mpu["UploadId"],
                MultipartUpload=part_info,
            )

        if self.fs.version_aware:
            self.version_id = write_result.get("VersionId")
        # complex cache invalidation, since file's appearance can cause several
        # directories
        self.buffer = None
        parts = self.path.split("/")
        path = parts[0]
        for p in parts[1:]:
            if path in self.fs.dircache and not [
                True for f in self.fs.dircache[path] if f["name"] == path + "/" + p
            ]:
                self.fs.invalidate_cache(path)
            path = path + "/" + p

    def discard(self):
        self._abort_mpu()
        self.buffer = None  # file becomes unusable

    def _abort_mpu(self):
        if self.mpu:
            self._call_s3(
                "abort_multipart_upload",
                Bucket=self.bucket,
                Key=self.key,
                UploadId=self.mpu["UploadId"],
            )
            self.mpu = None


def _fetch_range(fs, bucket, key, version_id, start, end, req_kw=None):
    if req_kw is None:
        req_kw = {}
    if start == end:
        logger.debug(
            "skip fetch for negative range - bucket=%s,key=%s,start=%d,end=%d",
            bucket,
            key,
            start,
            end,
        )
        return b""
    logger.debug("Fetch: %s/%s, %s-%s", bucket, key, start, end)
    resp = fs.call_s3(
        "get_object",
        Bucket=bucket,
        Key=key,
        Range="bytes=%i-%i" % (start, end - 1),
        **version_id_kw(version_id),
        **req_kw,
    )
    return sync(fs.loop, resp["Body"].read)
