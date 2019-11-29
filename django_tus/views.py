import base64
import logging
import os
import uuid

from django.core.cache import cache
from django.http import HttpResponse
from django.utils.decorators import method_decorator
from django.views.decorators.csrf import csrf_exempt
from django.views.generic import View

from django_tus.conf import settings
from django_tus.signals import tus_upload_finished_signal

logger = logging.getLogger(__name__)


def default_resource_id_generator(self):
    return str(uuid.uuid4())

class TusUpload(View):

    tus_api_version = '1.0.0'
    tus_api_version_supported = ['1.0.0', ]
    tus_api_extensions = ['creation', 'termination', 'file-check']
    on_finish = None
    resource_id_generator = default_resource_id_generator

    @method_decorator(csrf_exempt)
    def dispatch(self, *args, **kwargs):
        override_method = self.request.META.get('HTTP_X_HTTP_METHOD_OVERRIDE', None)
        if override_method:
            self.request.method = override_method
        logger.error("TUS dispatch", extra={'requestMETA': self.request.META, "requestMethod": self.request.method})

        return super(TusUpload, self).dispatch(*args, **kwargs)

    def get_tus_response(self):
        response = HttpResponse()
        response['Tus-Resumable'] = self.tus_api_version
        response['Tus-Version'] = ",".join(self.tus_api_version_supported)
        response['Tus-Extension'] = ",".join(self.tus_api_extensions)
        response['Tus-Max-Size'] = settings.TUS_MAX_FILE_SIZE
        response['Access-Control-Allow-Origin'] = "*"
        response['Access-Control-Allow-Methods'] = "PATCH,HEAD,GET,POST,OPTIONS"
        response['Access-Control-Expose-Headers'] = "Tus-Resumable,upload-length,upload-metadata,Location,Upload-Offset"
        response['Access-Control-Allow-Headers'] = "Tus-Resumable,upload-length,upload-metadata,Location,Upload-Offset,content-type"
        response['Cache-Control'] = 'no-store'

        return response

    def finished(self):
        if self.on_finish is not None:
            self.on_finish()

    def get(self, request, *args, **kwargs):

        """

        :param request:
        :param args:
        :param kwargs:
        :return response:
        """

        metadata = {}
        response = self.get_tus_response()

        if request.META.get("HTTP_TUS_RESUMABLE", None) is None:
            return HttpResponse(status=405, content="Method Not Allowed")

        for kv in request.META.get("HTTP_UPLOAD_METADATA", None).split(","):
            (key, value) = kv.split(" ")
            metadata[key] = base64.b64decode(value)

        if metadata.get("filename", None) and metadata.get(
                "filename").upper() in [f.upper() for f in os.listdir(os.path.dirname(settings.TUS_UPLOAD_DIR))]:
            response['Tus-File-Name'] = metadata.get("filename")
            response['Tus-File-Exists'] = True
        else:
            response['Tus-File-Exists'] = False
        return response

    def options(self, request, *args, **kwargs):
        """


        :param request:
        :param args:
        :param kwargs:
        :return response:
        """

        response = self.get_tus_response()
        response.status_code = 204
        return response

    def post(self, request, *args, **kwargs):

        """

        :param request:
        :param args:
        :param kwargs:
        :return:
        """
        response = self.get_tus_response()

        if request.META.get("HTTP_TUS_RESUMABLE", None) is None:
            # in dispatch auslagern?
            logger.warning("Received File upload for unsupported file transfer protocol")
            response.status_code = 500
            response.reason_phrase = "Received File upload for unsupported file transfer protocol"


        if request.method == 'OPTIONS':
            # eigene Methode
            response['Tus-Extension'] = ",".join(self.tus_api_extensions)
            response['Tus-Max-Size'] = settings.TUS_MAX_FILE_SIZE
            response.status_code = 204
            return response

        metadata = {}
        upload_metadata = request.META.get("HTTP_UPLOAD_METADATA", None)

        message_id = request.META.get("HTTP_MESSAGE_ID", None)
        if message_id:
            message_id = base64.b64decode(message_id)
            metadata["message_id"] = message_id
        logger.error("TUS Request", extra={'request': request.META})

        if upload_metadata:
            for kv in upload_metadata.split(","):
                (key, value) = kv.split(" ")
                metadata[key] = base64.b64decode(value).decode("utf-8")

        try:
            if os.path.lexists(
                    os.path.join(settings.TUS_UPLOAD_DIR, metadata.get("filename"))) and settings.TUS_FILE_OVERWRITE is False:
                response.status_code = 409
                return response
        except:
            logger.error("Unable to access file", extra={'request': request.META, 'metadata': metadata})
            #response.status_code = 409
            #return response
        file_name = metadata.get("filename")
        file_size = int(request.META.get("HTTP_UPLOAD_LENGTH", "0"))
        resource_id = self.resource_id_generator(file_name, request)
        cache.add("tus-uploads/{}/filename".format(resource_id), "{}".format(file_name), settings.TUS_TIMEOUT)
        cache.add("tus-uploads/{}/file_size".format(resource_id), file_size, settings.TUS_TIMEOUT)
        cache.add("tus-uploads/{}/offset".format(resource_id), 0, settings.TUS_TIMEOUT)
        cache.add("tus-uploads/{}/metadata".format(resource_id), metadata, settings.TUS_TIMEOUT)

        file_path = os.path.join(settings.TUS_UPLOAD_DIR, resource_id)
        os.makedirs(os.path.dirname(file_path), exist_ok=True)
        try:
            f = open(file_path, "wb")
            f.seek(file_size)
            f.write(b"\0")
            f.close()
        except IOError as e:
            logger.error("Unable to create file: {}".format(e), exc_info=True, extra={
            'request': request,
            })
            response.status_code = 500
            return response

        response.status_code = 201
        response['Location'] = '{}{}'.format(request.build_absolute_uri(), resource_id)
        return response

    def head(self, request, *args, **kwargs):
        response = self.get_tus_response()
        resource_id = kwargs.get('resource_id', None)

        offset = cache.get("tus-uploads/{}/offset".format(resource_id))
        file_size = cache.get("tus-uploads/{}/file_size".format(resource_id))
        if offset is None:
            response.status_code = 404
            return response

        else:
            response.status_code = 200
            response['Upload-Offset'] = offset
            response['Upload-Length'] = file_size

        return response

    def patch(self, request, *args, **kwargs):
        response = self.get_tus_response()

        resource_id = kwargs.get('resource_id', None)

        filename = cache.get("tus-uploads/{}/filename".format(resource_id))
        file_size = int(cache.get("tus-uploads/{}/file_size".format(resource_id)))
        metadata = cache.get("tus-uploads/{}/metadata".format(resource_id))
        offset = cache.get("tus-uploads/{}/offset".format(resource_id))


        file_offset = int(request.META.get("HTTP_UPLOAD_OFFSET", 0))
        chunk_size = int(request.META.get("CONTENT_LENGTH", 102400))

        upload_file_path = os.path.join(settings.TUS_UPLOAD_DIR, resource_id)
        if filename is None or os.path.lexists(upload_file_path) is False:
            response.status_code = 410
            return response

        if file_offset != offset:  # check to make sure we're in sync
            response.status_code = 409  # HTTP 409 Conflict
            return response

        logger.error("patch", extra={'request': self.request.META, 'tus': {
            "resource_id": resource_id,
            "filename": filename,
            "file_size": file_size,
            "metadata": metadata,
            "offset": offset,
            "upload_file_path": upload_file_path,
        }})


        try:
            file = open(upload_file_path, "r+b")
        except IOError:
            file = open(upload_file_path, "wb")
        finally:
            file.seek(file_offset)
            file.write(request.body)
            file.close()

        new_offset = cache.incr("tus-uploads/{}/offset".format(resource_id), chunk_size)
        response['Upload-Offset'] = new_offset

        response.status_code = 204

        logger.error("pre_finish_check")
        if file_size == new_offset:  # file transfer complete, rename from resource id to actual filename
            logger.error("post_finish_check")

            filename = uuid.uuid4().hex + "_" + filename
            destination_file_path = os.path.join(settings.TUS_DESTINATION_DIR, filename)
            os.rename(upload_file_path, destination_file_path)
            cache.delete_many([
                "tus-uploads/{}/file_size".format(resource_id),
                "tus-uploads/{}/filename".format(resource_id),
                "tus-uploads/{}/offset".format(resource_id),
                "tus-uploads/{}/metadata".format(resource_id),
            ])
            # sending signal
            tus_upload_finished_signal.send(
                sender=self.__class__,
                request=request,
                resource_id=resource_id,
                metadata=metadata,
                filename=filename,
                upload_file_path=upload_file_path,
                destination_file_path=destination_file_path,
                file_size=file_size,
                upload_url=settings.TUS_UPLOAD_URL,
                destination_folder=settings.TUS_DESTINATION_DIR)

            self.finished()
        return response
